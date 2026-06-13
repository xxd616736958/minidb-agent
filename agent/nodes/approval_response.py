"""Apply structured CLI approval responses to agent state."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from agent.state import AgentState
from collaboration.manager import CollaborationManager


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _status_for_action(action: str) -> str:
    if action == "approve":
        return "approved"
    if action == "edit_sql":
        return "edited"
    return "rejected"


def apply_cli_approval_response(state: AgentState) -> dict[str, Any]:
    """Convert a CLI approval response into durable approval state."""

    response = state.get("cli_approval_response") or {}
    pending = state.get("pending_approval") or {}
    if not response or not pending:
        return {"cli_approval_response": None}

    approval_id = str(response.get("approval_id") or "")
    if approval_id and approval_id != pending.get("id"):
        return {
            "cli_approval_response": None,
            "error": f"Approval response {approval_id} does not match pending approval {pending.get('id')}.",
        }

    action = str(response.get("action") or "reject")
    status = _status_for_action(action)
    sql_hash = response.get("sql_hash") or pending.get("sql_hash")
    sql_preview = response.get("sql") if action == "edit_sql" else pending.get("sql_preview")

    decision = {
        **pending,
        "status": status,
        "sql_hash": sql_hash,
        "sql_preview": sql_preview,
        "user_message": response.get("message") or action,
        "resolved_at": _now_iso(),
    }

    if status == "approved":
        update: dict[str, Any] = {
            "approval_decisions": [decision],
            "pending_approval": None,
            "approval_card": None,
            "cli_approval_response": None,
            "policy_violation": None,
            "loop_status": "running",
            "replan_trigger": None,
            "human_interrupt_pending": False,
        }
    else:
        update = {
            "approval_decisions": [decision],
            "pending_approval": None,
            "approval_card": None,
            "cli_approval_response": None,
            "policy_violation": {
                "message": f"User selected {action} for approval {pending.get('id')}.",
                "decision": status,
            },
            "loop_status": "blocked" if action == "reject" else "replanning",
            "replan_trigger": "approval_response",
        }

    update["collaboration_events"] = [
        CollaborationManager(state).event(
            "approval_resolved",
            f"Approval {pending.get('id')} resolved as {status}.",
            step_id=str(pending.get("step_id") or ""),
            payload_ref=str(pending.get("id") or ""),
        )
    ]
    return update
