"""Recovery decision engine for structured agent errors."""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Any

from agent.state import (
    AgentState,
    ErrorRecord,
    ErrorReport,
    RecoveryAttempt,
    RecoveryDecision,
    RetryBudget,
    StateIntegrityReport,
    StateRepairAction,
)


NEVER_RETRY = {
    "auth_error",
    "permission_denied",
    "deadlock_detected",
    "constraint_violation",
    "policy_denied",
    "approval_missing",
    "approval_mismatch",
}
SQL_REPAIR_TYPES = {"syntax_error", "sql_semantic_error", "object_not_found"}
DIAGNOSTIC_TYPES = {"lock_timeout", "statement_timeout"}
STATE_REPAIR_TYPES = {"state_integrity_error"}
ASK_USER_TYPES = {"auth_error", "permission_denied", "approval_missing", "approval_mismatch", "constraint_violation"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _compact(value: Any, limit: int = 500) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _scope_key(error: ErrorRecord) -> str:
    return "|".join(
        [
            str(error.get("step_id") or "none"),
            str(error.get("tool_name") or "none"),
            str(error.get("error_type") or "unknown"),
            str(error.get("sql_hash") or "none"),
        ]
    )


def _default_max_attempts(error_type: str) -> int:
    if error_type in NEVER_RETRY:
        return 0
    if error_type in {"tool_schema_error", "llm_output_error"}:
        return 1
    if error_type in {"connection_error", "tool_runtime_error", "statement_timeout"}:
        return 2
    if error_type in SQL_REPAIR_TYPES:
        return 1
    return 1


def _latest_budget(state: AgentState, scope_key: str) -> RetryBudget | None:
    for budget in reversed(state.get("retry_budgets", [])):
        if budget.get("scope_key") == scope_key:
            return budget
    return None


def _requires_new_approval(error: ErrorRecord, action: str) -> bool:
    if error.get("error_type") in {"approval_missing", "approval_mismatch"}:
        return True
    if action == "rewrite_sql" and error.get("sql_hash"):
        return True
    return False


def _decision_reason(error: ErrorRecord, action: str) -> str:
    error_type = error.get("error_type")
    if action == "auto_retry":
        return f"{error_type} appears transient and retry budget remains."
    if action == "rewrite_sql":
        return f"{error_type} can be repaired by rewriting SQL and re-running safety checks."
    if action == "run_diagnostic_tool":
        return f"{error_type} should be investigated with read-only PostgreSQL diagnostic tools."
    if action == "repair_state":
        return "State integrity error should be repaired before continuing inference."
    if action == "ask_user":
        return f"{error_type} requires user confirmation, credentials, approval, or a safer instruction."
    if action == "replan_step":
        return f"{error_type} blocks the current step but may be recoverable by replanning."
    return f"{error_type} is not safely recoverable automatically."


class RecoveryEngine:
    """Select recovery actions and build recovery state updates."""

    def __init__(self, state: AgentState | None = None) -> None:
        self.state = state or {}

    def update_retry_budget(self, error: ErrorRecord) -> RetryBudget:
        scope_key = _scope_key(error)
        current = _latest_budget(self.state, scope_key)
        max_attempts = (current or {}).get("max_attempts")
        if max_attempts is None:
            max_attempts = _default_max_attempts(str(error.get("error_type") or "unknown"))
        attempts = int((current or {}).get("attempts") or 0) + 1
        return {
            "scope_key": scope_key,
            "step_id": error.get("step_id"),
            "tool_name": error.get("tool_name"),
            "error_type": str(error.get("error_type") or "unknown"),
            "sql_hash": error.get("sql_hash"),
            "attempts": attempts,
            "max_attempts": int(max_attempts),
            "exhausted": attempts > int(max_attempts) if int(max_attempts) > 0 else True,
            "last_error_id": error.get("id"),
        }

    def decide(self, error: ErrorRecord, budget: RetryBudget | None = None) -> RecoveryDecision:
        error_type = str(error.get("error_type") or "unknown")
        budget = budget or self.update_retry_budget(error)
        action = "abort_safely"
        next_node: str | None = None
        confidence = 0.7
        safety_notes: list[str] = []

        if error_type in NEVER_RETRY:
            action = "ask_user" if error_type in ASK_USER_TYPES or error_type.startswith("approval") else "abort_safely"
            next_node = None
            safety_notes.append("Do not retry safety, approval, authentication, permission, or constraint errors automatically.")
        elif error_type in STATE_REPAIR_TYPES:
            action = "repair_state"
            next_node = "state_recovery"
            safety_notes.append("Repair state before continuing model inference.")
        elif error_type in SQL_REPAIR_TYPES:
            action = "rewrite_sql" if not budget.get("exhausted") else "replan_step"
            next_node = "llm_reason" if action == "rewrite_sql" else "task_planner"
            safety_notes.append("Any changed write SQL requires a new SQL hash and approval.")
        elif error_type in DIAGNOSTIC_TYPES:
            action = "run_diagnostic_tool" if not budget.get("exhausted") else "replan_step"
            next_node = "llm_reason" if action == "run_diagnostic_tool" else "task_planner"
            safety_notes.append("Use read-only diagnostics; do not immediately repeat the original SQL.")
        elif error_type in {"connection_error", "tool_runtime_error", "tool_schema_error", "llm_output_error"}:
            action = "auto_retry" if not budget.get("exhausted") else "ask_user"
            next_node = "llm_reason" if action == "auto_retry" else None
            safety_notes.append("Retry only while scoped retry budget remains.")
        elif error_type == "unknown":
            action = "replan_step" if not budget.get("exhausted") else "abort_safely"
            next_node = "task_planner" if action == "replan_step" else None
            safety_notes.append("Unknown errors should not be retried blindly after budget is exhausted.")

        return {
            "id": _new_id("recovery"),
            "error_id": str(error.get("id") or ""),
            "action": action,  # type: ignore[typeddict-item]
            "reason": _decision_reason(error, action),
            "confidence": confidence,
            "safety_notes": safety_notes,
            "requires_new_approval": _requires_new_approval(error, action),
            "next_node": next_node,
            "created_at": _now_iso(),
        }

    def attempt(
        self,
        error: ErrorRecord,
        decision: RecoveryDecision,
        *,
        status: str = "pending",
        summary: str | None = None,
    ) -> RecoveryAttempt:
        prior = [
            item
            for item in self.state.get("recovery_attempts", [])
            if item.get("error_id") == error.get("id")
        ]
        return {
            "id": _new_id("attempt"),
            "error_id": str(error.get("id") or ""),
            "decision_id": str(decision.get("id") or ""),
            "step_id": error.get("step_id"),
            "attempt_no": len(prior) + 1,
            "action": str(decision.get("action") or ""),
            "status": status,  # type: ignore[typeddict-item]
            "summary": _compact(summary or decision.get("reason") or ""),
            "created_at": _now_iso(),
            "completed_at": _now_iso() if status in {"succeeded", "failed", "skipped"} else None,
        }

    def repair_actions_from_integrity(
        self,
        report: StateIntegrityReport | None = None,
    ) -> list[StateRepairAction]:
        report = report or ((self.state.get("state_integrity_reports") or [None])[-1])
        if not report or report.get("ok"):
            return []
        actions: list[StateRepairAction] = []
        for description in report.get("repair_actions", []):
            lowered = str(description).lower()
            action_type = "mark_step_blocked"
            if "synchronize" in lowered or "sync" in lowered:
                action_type = "sync_plan_stack"
            elif "current_step" in lowered or "reset" in lowered:
                action_type = "reset_current_step"
            elif "approval_card" in lowered or "render pending_approval" in lowered:
                action_type = "regenerate_approval_card"
            elif "approval" in lowered and ("expire" in lowered or "remove" in lowered):
                action_type = "expire_pending_approval"
            elif "normalize_observation" in lowered or "tool_execution_result" in lowered:
                action_type = "normalize_tool_result"
            elif "step_context" in lowered:
                action_type = "refresh_step_context"
            actions.append(
                {
                    "id": _new_id("repair-action"),
                    "source_report_id": str(report.get("created_at") or ""),
                    "action_type": action_type,  # type: ignore[typeddict-item]
                    "description": str(description),
                    "status": "pending",
                    "created_at": _now_iso(),
                }
            )
        return actions

    def error_report(
        self,
        *,
        status: str,
        summary: str,
        next_options: list[str] | None = None,
    ) -> ErrorReport:
        plan = self.state.get("db_task_plan") or {}
        workspace = self.state.get("task_workspace") or {}
        error_ids = [item.get("id") for item in self.state.get("error_records", []) if item.get("id")]
        attempt_ids = [item.get("id") for item in self.state.get("recovery_attempts", []) if item.get("id")]
        evidence_refs = [
            *[item.get("id") for item in self.state.get("db_observations", []) if item.get("id")],
            *[item.get("id") for item in self.state.get("artifact_records", []) if item.get("id")],
        ]
        return {
            "id": _new_id("error-report"),
            "task_id": workspace.get("task_id"),
            "plan_id": plan.get("id"),
            "step_id": self.state.get("current_step_id"),
            "status": status,  # type: ignore[typeddict-item]
            "error_ids": [str(item) for item in error_ids],
            "recovery_attempt_ids": [str(item) for item in attempt_ids],
            "evidence_refs": [str(item) for item in evidence_refs],
            "user_summary": _compact(summary, 1000),
            "next_options": next_options
            or [
                "retry after fixing environment or credentials",
                "continue with read-only diagnostics",
                "generate a report-only summary",
                "revise the task plan",
            ],
            "created_at": _now_iso(),
        }

    def update_for_error(self, error: ErrorRecord) -> dict[str, Any]:
        budget = self.update_retry_budget(error)
        decision = self.decide(error, budget)
        attempt = self.attempt(error, decision)
        update: dict[str, Any] = {
            "error_records": [error],
            "recovery_decisions": [decision],
            "active_recovery_decision": decision,
            "recovery_attempts": [attempt],
            "retry_budgets": [
                *[
                    item
                    for item in self.state.get("retry_budgets", [])
                    if item.get("scope_key") != budget["scope_key"]
                ],
                budget,
            ],
        }
        if decision["action"] == "replan_step":
            update["replan_trigger"] = f"error:{error.get('error_type')}"
            update["loop_status"] = "replanning"
        if decision["action"] in {"ask_user", "abort_safely"}:
            update["loop_status"] = "blocked"
        if decision["action"] == "repair_state":
            update["state_repair_actions"] = self.repair_actions_from_integrity()
        return update
