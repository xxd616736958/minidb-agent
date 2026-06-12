"""Tool-call policy gate for PostgreSQL-safe tool invocation."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from agent.context import build_step_context_packet, retrieve_relevant_memories
from agent.state import (
    AgentState,
    RegisteredToolSpec,
    TaskStep,
    ToolCallPolicyDecision,
    ToolInvocationRecord,
)
from tools.catalog import current_step
from tools.registry import registry


WRITE_SQL_RE = re.compile(
    r"\b(insert|update|delete|merge|alter|drop|truncate|create|grant|revoke|vacuum|reindex)\b",
    re.IGNORECASE,
)
READ_SQL_RE = re.compile(r"\b(select|explain|show|with)\b", re.IGNORECASE)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def tool_call_items(msg: Any) -> list[dict[str, Any]]:
    return list(getattr(msg, "tool_calls", None) or [])


def tool_args_text(tool_call: dict[str, Any]) -> str:
    args = tool_call.get("args") or {}
    if isinstance(args, dict):
        return " ".join(str(value) for value in args.values())
    return str(args)


def is_postgres_tool(tool_name: str) -> bool:
    lowered = tool_name.lower()
    return any(token in lowered for token in ("postgres", "postgresql", "sql", "database", "db"))


def is_write_call(tool_call: dict[str, Any], spec: RegisteredToolSpec | None = None) -> bool:
    if spec and spec["capability"].get("read_only") is False:
        return True
    name = str(tool_call.get("name", ""))
    text = tool_args_text(tool_call)
    return is_postgres_tool(name) and bool(WRITE_SQL_RE.search(text))


def is_read_call(tool_call: dict[str, Any], spec: RegisteredToolSpec | None = None) -> bool:
    if spec and spec["capability"].get("read_only") is True:
        return True
    name = str(tool_call.get("name", ""))
    text = tool_args_text(tool_call)
    if not is_postgres_tool(name):
        return True
    return bool(READ_SQL_RE.search(text)) and not bool(WRITE_SQL_RE.search(text))


def _decision(
    tool_call: dict[str, Any],
    decision: str,
    reason: str,
    risk_level: str = "low",
    approval_payload: dict[str, Any] | None = None,
) -> ToolCallPolicyDecision:
    return {
        "call_id": str(tool_call.get("id", "")),
        "tool_name": str(tool_call.get("name", "unknown")),
        "decision": decision,  # type: ignore[typeddict-item]
        "reason": reason,
        "risk_level": risk_level,
        "approval_required": decision == "require_approval",
        "approval_payload": approval_payload,
    }


def _has_current_step_approval(state: AgentState, step: TaskStep | None) -> bool:
    if not step:
        return False
    return any(
        decision.get("step_id") == step.get("id") and decision.get("status") == "approved"
        for decision in state.get("approval_decisions", [])
    )


def _safety_memory_violation(state: AgentState, tool_call: dict[str, Any], spec: RegisteredToolSpec | None) -> str | None:
    if not is_write_call(tool_call, spec):
        return None

    memories = state.get("retrieved_memories")
    if memories is None:
        memories = retrieve_relevant_memories(state, limit=8)

    for memory in memories:
        if memory.get("kind") != "prohibition":
            continue
        text = f"{memory.get('summary', '')} {memory.get('payload', {})}".lower()
        if (
            "只读" in text
            or "read-only" in text
            or "read only" in text
            or "不要执行" in text
            or "禁止" in text
        ):
            return f"Long-term SafetyMemory prohibits write SQL: {memory.get('summary')}"

        blocked_verbs = ("drop", "truncate", "delete", "update", "insert", "alter", "grant", "revoke")
        call_text = tool_args_text(tool_call).lower()
        if any(verb in text and re.search(rf"\b{verb}\b", call_text, re.IGNORECASE) for verb in blocked_verbs):
            return f"Long-term SafetyMemory blocks this SQL pattern: {memory.get('summary')}"

    return None


def evaluate_tool_call(state: AgentState, tool_call: dict[str, Any]) -> ToolCallPolicyDecision:
    """Evaluate one tool call against current step, tool metadata, approvals, and memory."""
    tool_name = str(tool_call.get("name", "unknown"))
    spec = registry.get_spec(tool_name)
    step = current_step(state)
    packet = state.get("step_context")
    if step and packet and packet.get("step_id") != step.get("id"):
        packet = None
    packet = packet or build_step_context_packet(state)
    policy = str((packet or {}).get("tool_policy") or (step or {}).get("tool_policy", "no_tools"))
    risk = str((spec or {}).get("capability", {}).get("risk_level", (step or {}).get("risk_level", "low")))

    if policy == "no_tools":
        return _decision(tool_call, "deny", "Current plan step does not allow tool calls.", risk)

    if policy == "read_only_tools":
        if is_write_call(tool_call, spec):
            return _decision(tool_call, "deny", "Current plan step is read-only; write-capable tool call blocked.", risk)
        if spec and not is_read_call(tool_call, spec):
            return _decision(tool_call, "deny", "Current plan step allows only read-only database tools.", risk)

    safety_violation = _safety_memory_violation(state, tool_call, spec)
    if safety_violation:
        return _decision(tool_call, "deny", safety_violation, risk)

    if spec is None:
        return _decision(tool_call, "deny", f"Tool '{tool_name}' is not registered.", "high")

    if not spec.get("enabled", True):
        return _decision(tool_call, "deny", f"Tool '{tool_name}' is disabled.", risk)

    phase = str((step or {}).get("phase", ""))
    if phase and spec["allowed_phases"] and phase not in spec["allowed_phases"]:
        return _decision(
            tool_call,
            "deny",
            f"Tool '{tool_name}' is not allowed during phase '{phase}'.",
            risk,
        )

    if spec["allowed_policies"] and policy not in spec["allowed_policies"]:
        return _decision(
            tool_call,
            "deny",
            f"Tool '{tool_name}' is not allowed by current policy '{policy}'.",
            risk,
        )

    requires_approval = bool(spec["capability"].get("requires_approval")) or (
        policy == "write_tools_after_approval" and is_write_call(tool_call, spec)
    )
    if requires_approval and not _has_current_step_approval(state, step):
        return _decision(
            tool_call,
            "require_approval",
            "Current write-capable tool call requires explicit approval before execution.",
            risk,
            {
                "step_id": (step or {}).get("id"),
                "tool_name": tool_name,
                "tool_args": tool_call.get("args", {}),
            },
        )

    return _decision(tool_call, "allow", "allowed", risk)


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
        "error_type": None,
        "error_message": None,
    }
