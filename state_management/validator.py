"""State consistency validation for PostgreSQL task execution."""

from __future__ import annotations

from typing import Any

from agent.state import AgentState, StateIntegrityReport
from state_management.manager import now_iso


class StateValidator:
    """Validate internal consistency of AgentState."""

    def __init__(self, state: AgentState | None = None) -> None:
        self.state = state or {}

    def validate(self) -> StateIntegrityReport:
        state = self.state
        errors: list[str] = []
        warnings: list[str] = []
        repair_actions: list[str] = []

        plan = state.get("db_task_plan") or {}
        plan_steps = list(plan.get("steps", []) or [])
        stack_steps = list(state.get("task_stack", []) or [])
        steps = plan_steps or stack_steps
        step_ids = {step.get("id") for step in steps}
        current_step_id = state.get("current_step_id")

        if current_step_id and current_step_id not in step_ids:
            errors.append(f"current_step_id '{current_step_id}' is not present in task steps.")
            repair_actions.append("Reset current_step_id or re-run step scheduling.")

        if plan_steps and stack_steps:
            plan_ids = [step.get("id") for step in plan_steps]
            stack_ids = [step.get("id") for step in stack_steps]
            if plan_ids != stack_ids:
                errors.append("db_task_plan.steps and task_stack are not aligned.")
                repair_actions.append("Synchronize task_stack from db_task_plan.steps.")

        pending = state.get("pending_approval")
        if pending and current_step_id and pending.get("step_id") != current_step_id:
            errors.append("pending_approval is not bound to the current step.")
            repair_actions.append("Expire pending_approval or move current_step_id to the approved step.")

        intent = state.get("current_intent") or {}
        task_card = state.get("task_card") or {}
        if task_card and intent and task_card.get("intent_id") != intent.get("id"):
            warnings.append("task_card does not reference current_intent.")
            repair_actions.append("Regenerate task_card from current_intent.")

        plan_review = state.get("plan_review") or {}
        if plan_review and plan and plan_review.get("plan_id") != plan.get("id"):
            warnings.append("plan_review does not reference db_task_plan.")
            repair_actions.append("Regenerate plan_review from db_task_plan.")

        approval_card = state.get("approval_card") or {}
        if pending and approval_card and approval_card.get("approval_id") != pending.get("id"):
            errors.append("approval_card does not reference pending_approval.")
            repair_actions.append("Regenerate approval_card from pending_approval.")
        if pending and not approval_card:
            warnings.append("pending_approval has no human-facing approval_card.")
            repair_actions.append("Render pending_approval as ApprovalCard before waiting for user input.")

        db_env = state.get("database_environment") or {}
        runtime_policy = state.get("runtime_policy") or {}
        if db_env.get("is_production") and runtime_policy.get("allow_database_writes"):
            errors.append("Production database environment has database writes enabled.")
            repair_actions.append("Set runtime_policy.allow_database_writes to false.")
        if db_env.get("environment_name") == "unknown" and runtime_policy.get("allow_database_writes"):
            errors.append("Unknown database environment has database writes enabled.")
            repair_actions.append("Confirm target environment or keep runtime_policy.allow_database_writes false.")

        for approval in state.get("approval_decisions", []):
            if approval.get("status") != "approved":
                continue
            if approval.get("sql_preview") and not approval.get("sql_hash"):
                errors.append(f"approved approval '{approval.get('id')}' has SQL preview but no sql_hash.")
                repair_actions.append("Expire approval or bind it to a normalized SQL hash.")
            if approval.get("risk_level") in {"high", "critical"} and not approval.get("rollback_summary"):
                warnings.append(f"approved high-risk approval '{approval.get('id')}' has no rollback summary.")
                repair_actions.append("Add rollback summary before executing write SQL.")

        approved_ids = {approval.get("id") for approval in state.get("approval_decisions", []) if approval.get("status") == "approved"}
        for binding in state.get("approval_bindings", []):
            approval_id = binding.get("approval_id")
            if approval_id and approval_id not in approved_ids:
                errors.append(f"approval binding '{approval_id}' does not reference an approved decision.")
                repair_actions.append("Remove stale approval binding or request approval again.")

        observed_tool_call_ids = {
            obs.get("payload", {}).get("tool_call_id")
            for obs in state.get("db_observations", [])
            if obs.get("payload", {}).get("tool_call_id")
        }
        for result in state.get("tool_execution_results", []):
            call_id = result.get("tool_call_id")
            if call_id and call_id not in observed_tool_call_ids:
                warnings.append(f"tool_execution_result '{call_id}' has not been normalized into DBObservation.")
                repair_actions.append("Run normalize_observation before verification.")

        workspace_artifact_ids = set((state.get("task_workspace") or {}).get("artifact_ids", []) or [])
        for artifact in state.get("artifact_records", []):
            if artifact.get("id") not in workspace_artifact_ids:
                warnings.append(f"artifact '{artifact.get('id')}' is not referenced by task_workspace.")
                repair_actions.append("Attach artifact id to task_workspace.artifact_ids.")

        working_set = state.get("db_working_set") or {}
        if working_set.get("stale_reason"):
            warnings.append(f"db_working_set is stale: {working_set.get('stale_reason')}")

        return {
            "ok": not errors,
            "errors": errors,
            "warnings": warnings,
            "repair_actions": sorted(set(repair_actions)),
            "created_at": now_iso(),
        }

    def validation_update(self) -> dict[str, Any]:
        report = self.validate()
        return {"state_integrity_reports": [report]}
