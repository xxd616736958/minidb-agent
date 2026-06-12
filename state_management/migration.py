"""State schema migration for old LangGraph checkpoints."""

from __future__ import annotations

from typing import Any

from agent.state import AgentState
from state_management.manager import STATE_SCHEMA_VERSION, StateManager


class StateMigration:
    """Normalize older checkpoint states into the current schema."""

    def __init__(self, state: AgentState | None = None) -> None:
        self.state = state or {}

    def migrate(self) -> dict[str, Any]:
        state = self.state
        manager = StateManager(state)
        update: dict[str, Any] = {}
        if state.get("state_schema_version") != STATE_SCHEMA_VERSION or not state.get("state_metadata"):
            update.update(manager.metadata_update(recovery_mode="migrated"))

        defaults = {
            "db_observations": [],
            "approval_decisions": [],
            "verification_results": [],
            "result_digests": [],
            "context_snapshots": [],
            "tool_policy_decisions": [],
            "tool_invocation_records": [],
            "tool_execution_results": [],
            "artifact_records": [],
            "replay_policies": [],
        }
        for key, value in defaults.items():
            if key not in state:
                update[key] = value

        working_set = state.get("db_working_set")
        if working_set:
            working_set = dict(working_set)
            working_set.setdefault("source_observation_ids", [])
            working_set.setdefault("stale_reason", None)
            update["db_working_set"] = working_set

        migrated_state = {**state, **update}
        update["db_task_runtime"] = StateManager(migrated_state).build_runtime_state(migrated_state)
        return update
