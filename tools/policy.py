"""Tool-call policy gate for PostgreSQL-safe tool invocation."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any

from agent.state import (
    AgentState,
    RegisteredToolSpec,
    SQLSafetyReport,
    SecurityPolicyDecision,
    ToolCallPolicyDecision,
    ToolInvocationRecord,
)
from execution.environment import ExecutionEnvironmentManager
from safety.engine import (
    SecurityPolicyEngine,
    build_sql_safety_report,
    safety_decision_to_tool_decision,
)
from tools.catalog import current_step
from tools.registry import registry


POSTGRES_MUTATING_OPERATION_TYPES = {
    "data_change",
    "schema_change",
    "permission_change",
    "backup_restore",
    "maintenance",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def tool_call_items(msg: Any) -> list[dict[str, Any]]:
    return list(getattr(msg, "tool_calls", None) or [])


def tool_args_text(tool_call: dict[str, Any]) -> str:
    args = tool_call.get("args") or {}
    if isinstance(args, dict):
        return " ".join(str(value) for value in args.values())
    return str(args)


def sql_from_tool_call(tool_call: dict[str, Any]) -> str | None:
    args = tool_call.get("args") or {}
    if not isinstance(args, dict):
        return None
    for key in ("sql", "query", "statement"):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def is_postgres_tool(tool_name: str) -> bool:
    lowered = tool_name.lower()
    return any(token in lowered for token in ("postgres", "postgresql", "sql", "database", "db"))


def is_write_call(tool_call: dict[str, Any], spec: RegisteredToolSpec | None = None) -> bool:
    if spec:
        capability = spec["capability"]
        operation_type = capability.get("operation_type")
        return bool(capability.get("destructive")) or operation_type in {
            "data_change",
            "schema_change",
            "permission_change",
            "backup_restore",
            "maintenance",
        }
    sql = sql_from_tool_call(tool_call)
    if not sql or not is_postgres_tool(str(tool_call.get("name", ""))):
        return False
    report = build_sql_safety_report(sql, allow_explain_analyze=True)
    return report["classification"] in {
        "data_change",
        "schema_change",
        "permission_change",
        "maintenance",
        "transaction_control",
    }


def is_read_call(tool_call: dict[str, Any], spec: RegisteredToolSpec | None = None) -> bool:
    if spec and spec["capability"].get("read_only") is True:
        return True
    if not is_postgres_tool(str(tool_call.get("name", ""))):
        return True
    sql = sql_from_tool_call(tool_call)
    if not sql:
        return True
    report = build_sql_safety_report(sql)
    return report["classification"] in {"read_only", "diagnostic"} and not report["denial_reason"]


def evaluate_tool_call_full(
    state: AgentState,
    tool_call: dict[str, Any],
) -> tuple[ToolCallPolicyDecision, SecurityPolicyDecision, SQLSafetyReport | None, dict[str, Any] | None]:
    """Evaluate one tool call and return both public and internal safety records."""
    tool_name = str(tool_call.get("name", "unknown"))
    spec = registry.get_spec(tool_name)
    engine = SecurityPolicyEngine(state)
    safety_decision, sql_report, approval_binding = engine.evaluate_tool_call(tool_call, spec)
    tool_decision = safety_decision_to_tool_decision(
        safety_decision,
        call_id=str(tool_call.get("id", "")),
        tool_name=tool_name,
    )
    return tool_decision, safety_decision, sql_report, approval_binding


def evaluate_tool_call(state: AgentState, tool_call: dict[str, Any]) -> ToolCallPolicyDecision:
    """Evaluate one tool call against current step, tool metadata, approvals, and memory."""
    return evaluate_tool_call_full(state, tool_call)[0]


def evaluate_tool_calls(state: AgentState, tool_calls: list[dict[str, Any]]) -> list[ToolCallPolicyDecision]:
    return [evaluate_tool_call(state, call) for call in tool_calls]


def args_digest(args: Any) -> dict[str, Any]:
    text = json.dumps(args or {}, ensure_ascii=False, sort_keys=True, default=str)
    preview = text[:300]
    return {
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
        "preview": preview,
        "truncated": len(text) > len(preview),
    }


def make_invocation_record(
    state: AgentState,
    tool_call: dict[str, Any],
    decision: ToolCallPolicyDecision,
    status: str = "pending",
) -> ToolInvocationRecord:
    intent = state.get("current_intent") or {}
    step = current_step(state)
    return {
        "id": f"tool-{uuid.uuid4().hex[:12]}",
        "call_id": str(tool_call.get("id", "")),
        "tool_name": str(tool_call.get("name", "unknown")),
        "step_id": (step or {}).get("id"),
        "intent_id": intent.get("id"),
        "args_digest": args_digest(tool_call.get("args", {})),
        "policy_decision": decision,
        "approval_id": None,
        "started_at": now_iso(),
        "ended_at": None,
        "status": status,  # type: ignore[typeddict-item]
        "duration_ms": None,
        "result_ref": None,
        "observation_ids": [],
        "artifact_ids": [],
        "environment_summary": ExecutionEnvironmentManager(state).invocation_environment_summary(),
        "error_type": None,
        "error_message": None,
    }
