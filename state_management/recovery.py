"""Checkpoint recovery helpers for database task state."""

from __future__ import annotations

from typing import Any

from agent.state import AgentState
from execution.environment import ExecutionEnvironmentManager
from state_management.manager import StateManager
from state_management.migration import StateMigration
from state_management.validator import StateValidator


class StateRecovery:
    """Build a recoverable state update from a checkpoint state."""

    def __init__(self, state: AgentState | None = None) -> None:
        self.state = state or {}

    def recovery_summary(self, state: AgentState) -> str:
        runtime = state.get("db_task_runtime") or StateManager(state).build_runtime_state(state)
        observations = len(state.get("db_observations", []) or [])
        approvals = len(state.get("approval_decisions", []) or [])
        artifacts = len(state.get("artifact_records", []) or [])
        current = runtime.get("current_step_id") or "none"
        env = runtime.get("target_environment") or "unknown"
        database = runtime.get("target_database") or "unknown"
        status = runtime.get("task_status") or "new"
        return (
            f"Recovered task status={status}, step={current}, "
            f"target={env}/{database}, observations={observations}, "
            f"approvals={approvals}, artifacts={artifacts}."
        )

    def recover(self) -> dict[str, Any]:
        migrated = StateMigration(self.state).migrate()
        recovered_state = {**self.state, **migrated}

        environment_update = ExecutionEnvironmentManager(recovered_state).bootstrap_state()
        recovered_state = {**recovered_state, **environment_update}

        manager = StateManager(recovered_state)
        replay_policies = manager.replay_policies_for_records()
        metadata_update = manager.metadata_update(recovery_mode="resumed", last_node="state_recovery")
        runtime = manager.build_runtime_state({**recovered_state, **metadata_update})
        validation = StateValidator({**recovered_state, **metadata_update, "db_task_runtime": runtime}).validate()
        update: dict[str, Any] = {
            **migrated,
            **environment_update,
            **metadata_update,
            "db_task_runtime": runtime,
            "state_integrity_reports": [validation],
            "recovery_summary": self.recovery_summary({**recovered_state, **metadata_update, "db_task_runtime": runtime}),
        }
        if replay_policies:
            update["replay_policies"] = replay_policies
        return update
