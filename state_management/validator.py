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

        error_ids = {item.get("id") for item in state.get("error_records", [])}
        for decision in state.get("recovery_decisions", []):
            error_id = decision.get("error_id")
            if error_id and error_id not in error_ids:
                warnings.append(f"recovery decision '{decision.get('id')}' references missing error '{error_id}'.")
                repair_actions.append("Regenerate recovery decision from current ErrorRecord.")

        active_decision = state.get("active_recovery_decision") or {}
        if active_decision and active_decision.get("error_id") not in error_ids:
            warnings.append("active_recovery_decision references a missing ErrorRecord.")
            repair_actions.append("Clear active_recovery_decision or regenerate it from latest ErrorRecord.")
        if active_decision.get("action") == "auto_retry":
            for budget in state.get("retry_budgets", []):
                if budget.get("last_error_id") == active_decision.get("error_id") and budget.get("exhausted"):
                    errors.append("active_recovery_decision requests auto_retry but retry budget is exhausted.")
                    repair_actions.append("Switch recovery decision to ask_user, replan_step, or abort_safely.")

        role_names = {role.get("name") for role in state.get("agent_roles", [])}
        delegated_task_ids = {task.get("id") for task in state.get("delegated_tasks", [])}
        write_tool_names = {
            "postgres_execute_write",
            "postgres_analyze_table",
            "postgres_vacuum_table",
            "postgres_create_index_concurrently",
            "shell_execute",
        }
        for task in state.get("delegated_tasks", []):
            task_id = task.get("id")
            role_name = task.get("agent_role")
            parent_step_id = task.get("parent_step_id")
            if role_names and role_name not in role_names:
                errors.append(f"delegated_task '{task_id}' references unknown agent role '{role_name}'.")
                repair_actions.append("Regenerate delegated task from current AgentRoleDefinition.")
            if parent_step_id and step_ids and parent_step_id not in step_ids:
                errors.append(f"delegated_task '{task_id}' references missing parent step '{parent_step_id}'.")
                repair_actions.append("Cancel stale delegated task or re-run delegation planning.")
            allowed_tools = set(task.get("allowed_tools", []) or [])
            if allowed_tools & write_tool_names:
                errors.append(f"delegated_task '{task_id}' exposes write-capable tools to subagent.")
                repair_actions.append("Filter delegated task tools through AgentRoleDefinition.")
            if task.get("risk_level") in {"high", "critical"} and task.get("agent_role") != "safety_reviewer":
                warnings.append(f"delegated_task '{task_id}' is high risk and should be reviewed by safety_reviewer.")
                repair_actions.append("Create a safety_reviewer delegated task before approval.")

        for result in state.get("delegation_results", []):
            task_id = result.get("delegated_task_id")
            if task_id and task_id not in delegated_task_ids:
                errors.append(f"delegation_result '{result.get('id')}' references missing delegated task '{task_id}'.")
                repair_actions.append("Drop stale delegation result or restore the delegated task record.")
            if result.get("requires_human_review"):
                has_eval = any(
                    evaluation.get("result_id") == result.get("id")
                    for evaluation in state.get("delegation_evaluations", [])
                )
                if not has_eval:
                    warnings.append(f"delegation_result '{result.get('id')}' requires review but has no evaluation.")
                    repair_actions.append("Run delegation evaluation before using the result.")

        for evaluation in state.get("delegation_evaluations", []):
            result_id = evaluation.get("result_id")
            if result_id and not any(result.get("id") == result_id for result in state.get("delegation_results", [])):
                warnings.append(f"delegation_evaluation '{evaluation.get('id')}' references missing result '{result_id}'.")
                repair_actions.append("Regenerate delegation evaluation from current result.")

        profiles = {
            profile.get("model_id"): profile
            for profile in state.get("model_profiles", [])
        }
        route_ids = {route.get("id") for route in state.get("model_routes", [])}
        for route in state.get("model_routes", []):
            model_id = route.get("selected_model_id")
            profile = profiles.get(model_id)
            if profiles and not profile:
                errors.append(f"model route '{route.get('id')}' references unknown model '{model_id}'.")
                repair_actions.append("Regenerate model route from current ModelRegistry.")
            if route.get("task") == "tool_reasoning" and route.get("tools_bound") and profile and not profile.get("supports_tools"):
                errors.append(f"model route '{route.get('id')}' binds tools to a model without tool support.")
                repair_actions.append("Select a tool-capable model for tool_reasoning.")
            policy = route.get("policy") or {}
            if policy.get("require_review_model") and profile and profile.get("quality_tier") != "review":
                errors.append(f"model route '{route.get('id')}' requires review model but selected '{model_id}'.")
                repair_actions.append("Route high-risk model tasks to a review-tier model.")

        for record in state.get("model_invocation_records", []):
            route_id = record.get("route_id")
            if route_id and route_ids and route_id not in route_ids:
                warnings.append(f"model invocation '{record.get('id')}' references missing route '{route_id}'.")
                repair_actions.append("Persist ModelRoute before ModelInvocationRecord.")
            if record.get("task") == "tool_reasoning" and not record.get("tools_bound"):
                warnings.append(f"tool_reasoning invocation '{record.get('id')}' has no bound tools.")

        for fallback in state.get("model_fallback_decisions", []):
            if fallback.get("decision") == "downshift" and not fallback.get("allowed_by_policy"):
                errors.append(f"model fallback '{fallback.get('id')}' attempted disallowed downshift.")
                repair_actions.append("Fail closed or request user input for high-risk model fallback.")

        contract_ids = {contract.get("id") for contract in state.get("delivery_contracts", [])}
        manifest_ids = {manifest.get("id") for manifest in state.get("artifact_manifests", [])}
        delivery_artifact_ids = {artifact.get("id") for artifact in state.get("artifact_records", [])}
        for package in state.get("delivery_packages", []):
            package_id = package.get("id")
            if package.get("contract_id") and contract_ids and package.get("contract_id") not in contract_ids:
                errors.append(f"delivery package '{package_id}' references missing contract '{package.get('contract_id')}'.")
                repair_actions.append("Regenerate delivery package from active DeliveryContract.")
            if package.get("manifest_id") and manifest_ids and package.get("manifest_id") not in manifest_ids:
                errors.append(f"delivery package '{package_id}' references missing manifest '{package.get('manifest_id')}'.")
                repair_actions.append("Regenerate ArtifactManifest before delivery package.")
            if package.get("status") in {"ready", "delivered"} and not package.get("user_report_path"):
                errors.append(f"delivery package '{package_id}' is ready but has no user_report_path.")
                repair_actions.append("Regenerate final report and attach report path.")
            for artifact_id in package.get("artifact_ids", []) or []:
                if delivery_artifact_ids and artifact_id not in delivery_artifact_ids:
                    warnings.append(f"delivery package '{package_id}' references missing artifact '{artifact_id}'.")
                    repair_actions.append("Attach delivery artifacts to artifact_records.")

        for manifest in state.get("artifact_manifests", []):
            if manifest.get("missing_items") and any(package.get("manifest_id") == manifest.get("id") and package.get("status") == "ready" for package in state.get("delivery_packages", [])):
                warnings.append(f"artifact manifest '{manifest.get('id')}' has missing items but package is ready.")
                repair_actions.append("Run delivery_quality gate before marking package ready.")
            for sql_item in manifest.get("sql_items", []) or []:
                classification = sql_item.get("classification")
                if classification in {"data_change", "schema_change", "permission_change", "maintenance", "transaction_control"} and not sql_item.get("approval_id"):
                    errors.append(f"SQL delivery item '{sql_item.get('id')}' is write-classified but has no approval.")
                    repair_actions.append("Bind SQL delivery item to ApprovalDecision or block delivery.")
                if sql_item.get("sql_preview") and not sql_item.get("sql_hash"):
                    errors.append(f"SQL delivery item '{sql_item.get('id')}' has SQL preview but no hash.")
                    repair_actions.append("Compute sql_hash before delivery.")

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
