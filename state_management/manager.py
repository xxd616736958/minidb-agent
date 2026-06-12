"""State update helpers for the PostgreSQL-focused AgentState."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from agent.state import (
    AgentState,
    ArtifactRecord,
    DBObservation,
    DBTaskRuntimeState,
    ReplayPolicy,
    StateMetadata,
    TaskStep,
    VerificationResult,
)


STATE_SCHEMA_VERSION = 7
READONLY_REPLAYABLE_TOOLS = {
    "postgres_connection_check",
    "postgres_sql_classify",
    "postgres_list_schemas",
    "postgres_list_objects",
    "postgres_object_detail",
    "postgres_query_readonly",
    "postgres_explain",
    "postgres_top_queries",
    "postgres_health_check",
    "postgres_lock_inspect",
    "postgres_index_advisor",
    "postgres_hypothetical_index_test",
}
NON_REPLAYABLE_TOOLS = {
    "postgres_execute_write",
    "postgres_analyze_table",
    "postgres_vacuum_table",
    "postgres_create_index_concurrently",
    "shell_execute",
    "file_write",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class StateManager:
    """Centralized builders for AgentState partial updates."""

    def __init__(self, state: AgentState | None = None) -> None:
        self.state = state or {}

    @staticmethod
    def now() -> str:
        return now_iso()

    @staticmethod
    def new_id(prefix: str) -> str:
        return f"{prefix}-{uuid.uuid4().hex[:12]}"

    def metadata_update(
        self,
        *,
        last_node: str | None = None,
        last_transition: str | None = None,
        recovery_mode: str | None = None,
    ) -> dict[str, Any]:
        current = self.state.get("state_metadata") or {}
        created_at = current.get("created_at") or now_iso()
        metadata: StateMetadata = {
            "schema_version": STATE_SCHEMA_VERSION,
            "session_id": str(self.state.get("session_id") or current.get("session_id") or ""),
            "created_at": created_at,
            "updated_at": now_iso(),
            "last_node": last_node if last_node is not None else current.get("last_node"),
            "last_transition": last_transition if last_transition is not None else current.get("last_transition"),
            "recovery_mode": recovery_mode or current.get("recovery_mode") or "normal",  # type: ignore[typeddict-item]
        }
        return {
            "state_schema_version": STATE_SCHEMA_VERSION,
            "state_metadata": metadata,
        }

    def plan_steps(self) -> list[TaskStep]:
        plan = self.state.get("db_task_plan")
        if plan and plan.get("steps"):
            return [dict(step) for step in plan["steps"]]  # type: ignore[list-item]
        return [dict(step) for step in self.state.get("task_stack", [])]  # type: ignore[list-item]

    def find_step_index(self, steps: list[TaskStep], step_id: str | None = None) -> int:
        target = step_id if step_id is not None else self.state.get("current_step_id")
        if target:
            for idx, step in enumerate(steps):
                if step.get("id") == target:
                    return idx
        idx = int(self.state.get("current_task_index") or 0)
        if steps and idx < len(steps):
            return idx
        return 0

    def current_step(self, steps: list[TaskStep] | None = None) -> TaskStep | None:
        steps = steps if steps is not None else self.plan_steps()
        if not steps:
            return None
        idx = self.find_step_index(steps)
        return steps[idx] if idx < len(steps) else None

    def sync_plan_and_stack(
        self,
        steps: list[TaskStep],
        current_idx: int | None = None,
        plan_status: str | None = None,
    ) -> dict[str, Any]:
        update: dict[str, Any] = {
            "task_stack": steps,
        }
        if current_idx is not None:
            update["current_task_index"] = current_idx
            if 0 <= current_idx < len(steps):
                update["current_step_id"] = steps[current_idx].get("id")

        plan = self.state.get("db_task_plan")
        if plan:
            plan = dict(plan)
            plan["steps"] = steps
            plan["updated_at"] = now_iso()
            if plan_status:
                plan["status"] = plan_status
            update["db_task_plan"] = plan
        update.update(self.metadata_update(last_transition="sync_plan_and_stack"))
        update["db_task_runtime"] = self.build_runtime_state({**self.state, **update})
        return update

    def build_runtime_state(self, state: AgentState | None = None) -> DBTaskRuntimeState:
        state = state or self.state
        intent = state.get("current_intent") or {}
        plan = state.get("db_task_plan") or {}
        db_env = state.get("database_environment") or {}
        steps = StateManager(state).plan_steps()
        step = StateManager(state).current_step(steps)
        loop_status = state.get("loop_status")
        if loop_status == "completed":
            task_status = "completed"
        elif loop_status == "blocked" or state.get("policy_violation"):
            task_status = "blocked"
        elif state.get("pending_approval"):
            task_status = "waiting"
        elif plan:
            task_status = "running"
        elif intent:
            task_status = "planning"
        else:
            task_status = "new"
        return {
            "intent_id": intent.get("id"),
            "plan_id": plan.get("id"),
            "current_step_id": state.get("current_step_id"),
            "current_phase": (step or {}).get("phase"),
            "target_environment": str(
                db_env.get("environment_name")
                or intent.get("target_environment")
                or "unknown"
            ),
            "target_database": db_env.get("target_database") or intent.get("target_database"),
            "risk_level": str((step or {}).get("risk_level") or plan.get("global_risk_level") or intent.get("risk_level") or "unknown"),
            "task_status": task_status,  # type: ignore[typeddict-item]
            "blocked_reason": (state.get("policy_violation") or {}).get("message") or state.get("error"),
        }

    def runtime_update(self, *, last_node: str | None = None) -> dict[str, Any]:
        state = self.state
        update = self.metadata_update(last_node=last_node)
        update["db_task_runtime"] = self.build_runtime_state(state)
        return update

    def record_observations(
        self,
        observations: list[DBObservation],
        digests: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        snapshot_state = {
            **self.state,
            "db_observations": [*self.state.get("db_observations", []), *observations],
            "result_digests": [*self.state.get("result_digests", []), *(digests or [])],
        }
        update: dict[str, Any] = {
            "db_observations": observations,
            "result_digests": digests or [],
            **self.metadata_update(last_node="normalize_observation"),
            "db_task_runtime": self.build_runtime_state(snapshot_state),
        }
        return update

    def record_verification(
        self,
        result: VerificationResult,
        artifact: ArtifactRecord | None = None,
        environment_update: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        update: dict[str, Any] = {
            "verification_results": [result],
            **self.metadata_update(last_node="verify_step"),
        }
        if artifact:
            update["artifact_records"] = [artifact]
        if environment_update:
            update.update(environment_update)
        merged = {**self.state, **update}
        update["db_task_runtime"] = self.build_runtime_state(merged)
        return update

    def record_artifacts_on_task_workspace(
        self,
        artifact_records: list[ArtifactRecord],
        task_workspace: dict[str, Any] | None,
    ) -> dict[str, Any]:
        if not artifact_records or not task_workspace:
            return {}
        workspace = dict(task_workspace)
        workspace["artifact_ids"] = [
            *list(workspace.get("artifact_ids", [])),
            *(artifact["id"] for artifact in artifact_records),
        ]
        workspace["updated_at"] = now_iso()
        return {"task_workspace": workspace}

    def replay_policy_for_tool(self, tool_name: str, call_id: str) -> ReplayPolicy:
        if tool_name in READONLY_REPLAYABLE_TOOLS:
            return {
                "tool_call_id": call_id,
                "replayable": True,
                "reason": "Read-only PostgreSQL inspection tool can be safely rerun.",
                "requires_new_approval": False,
            }
        if tool_name in NON_REPLAYABLE_TOOLS:
            return {
                "tool_call_id": call_id,
                "replayable": False,
                "reason": "Tool may mutate files, shell state, or database state and must not auto-replay.",
                "requires_new_approval": True,
            }
        return {
            "tool_call_id": call_id,
            "replayable": False,
            "reason": "Unknown replay safety; require explicit user or policy approval.",
            "requires_new_approval": True,
        }

    def replay_policies_for_records(self) -> list[ReplayPolicy]:
        existing = {item.get("tool_call_id") for item in self.state.get("replay_policies", [])}
        policies: list[ReplayPolicy] = []
        for record in self.state.get("tool_invocation_records", []):
            call_id = str(record.get("call_id") or "")
            if not call_id or call_id in existing:
                continue
            policies.append(self.replay_policy_for_tool(str(record.get("tool_name") or ""), call_id))
        return policies

    def mark_blocked(self, reason: str) -> dict[str, Any]:
        update = {
            "loop_status": "blocked",
            "replan_trigger": "state_integrity_blocked",
            "error": reason,
            **self.metadata_update(last_transition="blocked"),
        }
        update["db_task_runtime"] = self.build_runtime_state({**self.state, **update})
        return update
