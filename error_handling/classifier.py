"""Error classification for PostgreSQL-focused agent recovery."""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Any

from agent.state import AgentState, ErrorRecord, StateIntegrityReport, ToolExecutionResult


SQLSTATE_ERROR_TYPES = {
    "08000": "connection_error",
    "08001": "connection_error",
    "08003": "connection_error",
    "08006": "connection_error",
    "28P01": "auth_error",
    "42501": "permission_denied",
    "42601": "syntax_error",
    "42703": "sql_semantic_error",
    "42P01": "object_not_found",
    "42P07": "sql_semantic_error",
    "57014": "statement_timeout",
    "55P03": "lock_timeout",
    "40P01": "deadlock_detected",
    "23502": "constraint_violation",
    "23503": "constraint_violation",
    "23505": "constraint_violation",
    "23514": "constraint_violation",
}

USER_ACTION_TYPES = {
    "auth_error",
    "permission_denied",
    "deadlock_detected",
    "constraint_violation",
    "policy_denied",
    "approval_missing",
    "approval_mismatch",
}

RETRYABLE_TYPES = {
    "connection_error",
    "statement_timeout",
    "tool_runtime_error",
    "tool_schema_error",
    "llm_output_error",
}

CRITICAL_TYPES = {
    "approval_mismatch",
    "deadlock_detected",
    "state_integrity_error",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _compact(value: Any, limit: int = 500) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _current_step_id(state: AgentState) -> str | None:
    return state.get("current_step_id")


def _target_env(state: AgentState) -> tuple[str, str | None]:
    db_env = state.get("database_environment") or {}
    intent = state.get("current_intent") or {}
    return (
        str(db_env.get("environment_name") or intent.get("target_environment") or "unknown"),
        db_env.get("target_database") or intent.get("target_database"),
    )


def _sql_hash_for_tool_call(state: AgentState, tool_call_id: str | None) -> str | None:
    if not tool_call_id:
        return None
    for report in reversed(state.get("sql_safety_reports", [])):
        payload = report.get("payload", {}) if isinstance(report.get("payload"), dict) else {}
        if payload.get("tool_call_id") == tool_call_id:
            return report.get("sql_hash")
    for record in reversed(state.get("tool_invocation_records", [])):
        if record.get("call_id") == tool_call_id:
            decision = record.get("policy_decision") or {}
            payload = decision.get("approval_payload") or {}
            return payload.get("sql_hash")
    return None


def classify_text(message: str, *, fallback: str = "unknown") -> str:
    """Classify an error message using conservative PostgreSQL-aware rules."""
    text = message.lower()
    if "approval" in text and ("hash" in text or "mismatch" in text or "environment" in text):
        return "approval_mismatch"
    if "approval" in text and ("missing" in text or "required" in text or "requires" in text):
        return "approval_missing"
    if "policy_denied" in text or "policy denied" in text or "safety" in text and "blocked" in text:
        return "policy_denied"
    if "permission denied" in text or "insufficient privilege" in text or "42501" in text:
        return "permission_denied"
    if "authentication" in text or "password authentication failed" in text or "28p01" in text:
        return "auth_error"
    if "could not connect" in text or "connection" in text and any(token in text for token in ("refused", "failed", "closed", "timeout")):
        return "connection_error"
    if "syntax error" in text or "42601" in text:
        return "syntax_error"
    if "does not exist" in text or "undefined table" in text or "42p01" in text:
        return "object_not_found"
    if "undefined column" in text or "42703" in text or "operator does not exist" in text:
        return "sql_semantic_error"
    if "lock timeout" in text or "could not obtain lock" in text or "55p03" in text:
        return "lock_timeout"
    if "statement timeout" in text or "canceling statement due to statement timeout" in text or "57014" in text:
        return "statement_timeout"
    if "deadlock" in text or "40p01" in text:
        return "deadlock_detected"
    if "duplicate key" in text or "violates" in text and "constraint" in text:
        return "constraint_violation"
    if "tool_calls must be followed" in text or "invalid tool" in text or "tool schema" in text:
        return "tool_schema_error"
    if "llm" in text or "model" in text:
        return "llm_output_error"
    if "state" in text and ("not aligned" in text or "not present" in text or "integrity" in text):
        return "state_integrity_error"
    if "error" in text or "exception" in text:
        return "tool_runtime_error"
    return fallback


def _source_for_error_type(error_type: str, *, default: str = "system") -> str:
    if error_type in {
        "connection_error",
        "auth_error",
        "permission_denied",
        "syntax_error",
        "sql_semantic_error",
        "object_not_found",
        "lock_timeout",
        "statement_timeout",
        "deadlock_detected",
        "constraint_violation",
    }:
        return "postgresql"
    if error_type in {"policy_denied"}:
        return "safety_policy"
    if error_type in {"approval_missing", "approval_mismatch"}:
        return "approval"
    if error_type == "state_integrity_error":
        return "state"
    if error_type in {"tool_schema_error", "tool_runtime_error"}:
        return "tool"
    if error_type == "llm_output_error":
        return "llm"
    return default


def _severity(error_type: str) -> str:
    if error_type in CRITICAL_TYPES:
        return "critical"
    if error_type in {"policy_denied", "approval_missing", "approval_mismatch"}:
        return "warning"
    if error_type == "unknown":
        return "warning"
    return "error"


class ErrorClassifier:
    """Create structured ErrorRecord objects from state and execution artifacts."""

    def __init__(self, state: AgentState | None = None) -> None:
        self.state = state or {}

    def record(
        self,
        *,
        message: str,
        source: str | None = None,
        error_type: str | None = None,
        node_name: str | None = None,
        step_id: str | None = None,
        tool_name: str | None = None,
        tool_call_id: str | None = None,
        sql_hash: str | None = None,
        sqlstate: str | None = None,
        raw_excerpt: str | None = None,
    ) -> ErrorRecord:
        if not error_type and sqlstate:
            error_type = SQLSTATE_ERROR_TYPES.get(str(sqlstate).upper())
        if not error_type:
            error_type = classify_text(message)
        target_environment, target_database = _target_env(self.state)
        if not sql_hash:
            sql_hash = _sql_hash_for_tool_call(self.state, tool_call_id)
        retryable = error_type in RETRYABLE_TYPES
        requires_user_action = error_type in USER_ACTION_TYPES
        return {
            "id": _new_id("err"),
            "source": source or _source_for_error_type(error_type),  # type: ignore[typeddict-item]
            "error_type": error_type,  # type: ignore[typeddict-item]
            "severity": _severity(error_type),  # type: ignore[typeddict-item]
            "node_name": node_name,
            "step_id": step_id or _current_step_id(self.state),
            "tool_name": tool_name,
            "tool_call_id": tool_call_id,
            "sql_hash": sql_hash,
            "sqlstate": sqlstate,
            "target_environment": target_environment,
            "target_database": target_database,
            "message": _compact(message, 500),
            "raw_excerpt": _compact(raw_excerpt or message, 1000),
            "retryable": retryable,
            "requires_user_action": requires_user_action,
            "created_at": _now_iso(),
        }

    def from_state_error(self, *, node_name: str | None = None) -> ErrorRecord | None:
        error = self.state.get("error")
        if not error:
            return None
        return self.record(
            message=str(error),
            node_name=node_name,
            source=None,
        )

    def from_tool_result(self, result: ToolExecutionResult) -> ErrorRecord | None:
        if result.get("success") is True:
            return None
        result_type = str(result.get("result_type") or "tool_error")
        sqlstate = result.get("sqlstate")
        error_type = SQLSTATE_ERROR_TYPES.get(str(sqlstate).upper()) if sqlstate else None
        if not error_type:
            if result_type == "policy_denied":
                error_type = "policy_denied"
            elif result_type == "sql_error":
                error_type = classify_text(str(result.get("summary") or ""), fallback="sql_semantic_error")
            else:
                error_type = classify_text(str(result.get("summary") or ""), fallback="tool_runtime_error")
        return self.record(
            message=str(result.get("summary") or result_type),
            source=_source_for_error_type(error_type, default="tool"),
            error_type=error_type,
            node_name="execute_tools",
            tool_name=result.get("tool_name"),
            tool_call_id=result.get("tool_call_id"),
            sqlstate=sqlstate,
            raw_excerpt=str(result.get("payload") or result.get("summary") or ""),
        )

    def from_policy_violation(self) -> ErrorRecord | None:
        violation = self.state.get("policy_violation")
        if not violation:
            return None
        message = str(violation.get("message") or "Tool call blocked by policy.")
        decisions = violation.get("decisions") or []
        first = decisions[0] if decisions else {}
        error_type = "approval_missing" if first.get("decision") == "require_approval" else "policy_denied"
        return self.record(
            message=message,
            source="safety_policy",
            error_type=error_type,
            node_name="tool_policy_gate",
            step_id=violation.get("step_id"),
            tool_name=", ".join(str(item) for item in violation.get("blocked_tools", [])) or None,
            raw_excerpt=str(violation),
        )

    def from_integrity_report(self, report: StateIntegrityReport | None = None) -> ErrorRecord | None:
        report = report or ((self.state.get("state_integrity_reports") or [None])[-1])
        if not report or report.get("ok"):
            return None
        errors = report.get("errors") or []
        warnings = report.get("warnings") or []
        message = "; ".join(str(item) for item in (errors or warnings)[:3])
        return self.record(
            message=message or "State integrity check failed.",
            source="state",
            error_type="state_integrity_error",
            node_name="state_validator",
            raw_excerpt=str(report),
        )
