"""Graph node for controlled multi-agent delegation planning."""

from __future__ import annotations

from typing import Any

from agent.state import AgentState
from delegation.manager import DelegationManager
from state_management.validator import StateValidator


def delegation_planner(state: AgentState) -> dict[str, Any]:
    """Prepare specialist subagent delegation state for the current plan.

    This node does not execute subagents. It only creates auditable role,
    decision, delegated task, and team-run records that downstream execution
    and quality gates can consume.
    """
    if state.get("error"):
        return {}
    if not state.get("db_task_plan") and not state.get("task_stack"):
        return DelegationManager(state).roles_update()

    update = DelegationManager(state).planning_update()
    validation_state = {**state, **update}
    update.update(StateValidator(validation_state).validation_update())
    return update
