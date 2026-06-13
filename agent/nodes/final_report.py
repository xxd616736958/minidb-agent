"""Final artifact delivery node for PostgreSQL task workflows."""

from __future__ import annotations

import logging
from typing import Any

from agent.state import AgentState
from collaboration.manager import CollaborationManager
from delivery.manager import DeliveryManager
from state_management.manager import StateManager
from state_management.validator import StateValidator

logger = logging.getLogger(__name__)


def _should_skip_delivery(state: AgentState) -> bool:
    """Avoid duplicate delivery packages for the same terminal state."""
    packages = state.get("delivery_packages", [])
    if not packages:
        return False
    latest = packages[-1]
    active_contract = state.get("active_delivery_contract") or {}
    plan = state.get("db_task_plan") or {}
    if plan.get("id") and active_contract.get("plan_id") != plan.get("id"):
        return False
    runtime = state.get("db_task_runtime") or {}
    status = latest.get("status")
    if status in {"ready", "blocked", "failed"} and runtime.get("task_status") in {"completed", "blocked"}:
        return True
    return False


def final_report(state: AgentState) -> dict[str, Any]:
    """Generate delivery contract, manifest, reports, quality gate, and package."""
    if _should_skip_delivery(state):
        return {}

    runtime = state.get("db_task_runtime") or {}
    task_status = runtime.get("task_status") or state.get("loop_status")
    blocking_error_reports = [
        report
        for report in state.get("error_reports", []) or []
        if report.get("status") in {"failed", "blocked"}
    ]
    force_blocked = task_status in {"blocked", "failed", "error"} or bool(blocking_error_reports)
    try:
        update = DeliveryManager(state).build_delivery_update(force_blocked=force_blocked)
    except Exception as exc:
        logger.exception("Final delivery generation failed")
        return {
            "error": f"Final delivery generation failed: {exc}",
            **StateManager(state).runtime_update(last_node="final_report"),
        }

    package = update.get("delivery_packages", [{}])[-1]
    event = CollaborationManager(state).event(
        "final_report_shown",
        f"Delivery package {package.get('status')}: {package.get('summary')}",
        payload_ref=package.get("id"),
    )
    update["collaboration_events"] = [event]
    next_state = {**state, **update}
    update.update(StateManager(next_state).runtime_update(last_node="final_report"))
    update.update(StateValidator(next_state).validation_update())
    return update
