"""Controlled subagent roles, task delegation, and result evaluation."""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Any

from agent.context import current_step, normalize_user_constraints
from agent.state import (
    AgentRoleDefinition,
    AgentState,
    AgentTeamRun,
    DelegatedTask,
    DelegationEvaluation,
    DelegationFailure,
    DelegationPolicyDecision,
    DelegationRecord,
    DelegationResult,
    RegisteredToolSpec,
    TaskStep,
)
from state_management.manager import StateManager


READ_ONLY_TOOL_NAMES = [
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
    "postgres_dry_run",
]
WRITE_TOOL_NAMES = [
    "postgres_execute_write",
    "postgres_analyze_table",
    "postgres_vacuum_table",
    "postgres_create_index_concurrently",
    "shell_execute",
]
HIGH_RISK_OPERATION_TYPES = {
    "schema_change",
    "data_change",
    "permission_change",
    "backup_restore",
    "maintenance",
}
PERFORMANCE_HINT_RE = re.compile(
    r"(slow|performance|latency|index|explain|pg_stat|慢|性能|索引|执行计划)",
    re.IGNORECASE,
)
SCHEMA_HINT_RE = re.compile(r"(schema|table|column|constraint|dependency|结构|表|字段|约束|依赖)", re.IGNORECASE)
REPORT_HINT_RE = re.compile(r"(report|summary|document|巡检|报告|文档|总结)", re.IGNORECASE)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _risk_rank(value: str | None) -> int:
    return {"low": 1, "medium": 2, "high": 3, "critical": 4}.get(str(value or "low"), 1)


def _risk_at_least(value: str | None, threshold: str) -> bool:
    return _risk_rank(value) >= _risk_rank(threshold)


def _dedupe(items: list[str]) -> list[str]:
    return list(dict.fromkeys(item for item in items if item))


def _step_text(step: TaskStep | None) -> str:
    if not step:
        return ""
    return " ".join(
        str(value)
        for value in (
            step.get("id"),
            step.get("description"),
            step.get("phase"),
            step.get("operation_type"),
            " ".join(step.get("evidence_required", []) or []),
            " ".join(step.get("success_criteria", []) or []),
        )
        if value
    )


def default_agent_roles() -> list[AgentRoleDefinition]:
    """Return the fixed first-phase PostgreSQL specialist roles."""
    return [
        {
            "id": "role-schema-explorer",
            "name": "schema_explorer",
            "description": "Inspect PostgreSQL schemas, tables, columns, indexes, constraints, and dependencies.",
            "responsibilities": [
                "Map relevant database objects.",
                "Find dependencies and object details.",
                "Collect schema evidence for planning and impact analysis.",
            ],
            "allowed_tools": [
                "postgres_connection_check",
                "postgres_list_schemas",
                "postgres_list_objects",
                "postgres_object_detail",
                "postgres_query_readonly",
            ],
            "disallowed_tools": WRITE_TOOL_NAMES,
            "allowed_phases": ["observe", "diagnose", "propose"],
            "default_model": None,
            "max_turns": 3,
            "max_tool_calls": 5,
            "permission_mode": "read_only",
            "memory_scope": "task",
            "can_run_in_parallel": True,
            "output_schema": "DelegationResult",
        },
        {
            "id": "role-performance-analyst",
            "name": "performance_analyst",
            "description": "Analyze slow SQL, execution plans, index options, statistics, locks, and query health.",
            "responsibilities": [
                "Collect performance evidence.",
                "Explain likely bottlenecks.",
                "Recommend read-only diagnostic or proposal-only tuning actions.",
            ],
            "allowed_tools": [
                "postgres_query_readonly",
                "postgres_explain",
                "postgres_dry_run",
                "postgres_top_queries",
                "postgres_health_check",
                "postgres_lock_inspect",
                "postgres_index_advisor",
                "postgres_hypothetical_index_test",
            ],
            "disallowed_tools": WRITE_TOOL_NAMES,
            "allowed_phases": ["observe", "diagnose", "propose", "verify"],
            "default_model": None,
            "max_turns": 4,
            "max_tool_calls": 7,
            "permission_mode": "read_only",
            "memory_scope": "task",
            "can_run_in_parallel": True,
            "output_schema": "DelegationResult",
        },
        {
            "id": "role-safety-reviewer",
            "name": "safety_reviewer",
            "description": "Review PostgreSQL risk, approval materials, rollback plans, and evidence completeness.",
            "responsibilities": [
                "Check whether proposed actions are backed by evidence.",
                "Find missing approval, rollback, backup, and verification material.",
                "Flag high-risk or ambiguous recommendations for human review.",
            ],
            "allowed_tools": [
                "postgres_sql_classify",
                "postgres_query_readonly",
                "postgres_explain",
                "postgres_dry_run",
                "postgres_object_detail",
            ],
            "disallowed_tools": WRITE_TOOL_NAMES,
            "allowed_phases": ["propose", "approve", "verify", "report"],
            "default_model": None,
            "max_turns": 4,
            "max_tool_calls": 5,
            "permission_mode": "review_only",
            "memory_scope": "task",
            "can_run_in_parallel": False,
            "output_schema": "DelegationResult",
        },
        {
            "id": "role-migration-planner",
            "name": "migration_planner",
            "description": "Draft migration plans, rollback strategies, verification criteria, and deployment sequencing.",
            "responsibilities": [
                "Prepare proposal-only migration steps.",
                "Document rollback and verification criteria.",
                "Identify dependencies and operational risks.",
            ],
            "allowed_tools": [
                "postgres_sql_classify",
                "postgres_list_objects",
                "postgres_object_detail",
                "postgres_query_readonly",
                "postgres_explain",
            ],
            "disallowed_tools": WRITE_TOOL_NAMES,
            "allowed_phases": ["diagnose", "propose", "approve"],
            "default_model": None,
            "max_turns": 4,
            "max_tool_calls": 5,
            "permission_mode": "proposal_only",
            "memory_scope": "task",
            "can_run_in_parallel": False,
            "output_schema": "DelegationResult",
        },
        {
            "id": "role-report-writer",
            "name": "report_writer",
            "description": "Summarize evidence into user-facing PostgreSQL diagnostic, audit, or task reports.",
            "responsibilities": [
                "Assemble findings and evidence references.",
                "Adapt output for DBA, developer, or operational audiences.",
                "Call out missing evidence and open questions.",
            ],
            "allowed_tools": [],
            "disallowed_tools": WRITE_TOOL_NAMES,
            "allowed_phases": ["report"],
            "default_model": None,
            "max_turns": 2,
            "max_tool_calls": 0,
            "permission_mode": "review_only",
            "memory_scope": "task",
            "can_run_in_parallel": True,
            "output_schema": "DelegationResult",
        },
    ]


class DelegationManager:
    """Build and validate controlled PostgreSQL subagent delegation state."""

    def __init__(self, state: AgentState | None = None) -> None:
        self.state = state or {}
        self._roles = {
            role["name"]: role
            for role in (self.state.get("agent_roles") or default_agent_roles())
        }

    def roles(self) -> list[AgentRoleDefinition]:
        return list(self._roles.values())

    def role(self, name: str) -> AgentRoleDefinition | None:
        return self._roles.get(name)

    def roles_update(self) -> dict[str, Any]:
        if self.state.get("agent_roles"):
            return {}
        return {"agent_roles": default_agent_roles()}

    def policy_decision(self, step: TaskStep | None = None) -> DelegationPolicyDecision:
        step = step or current_step(self.state) or {}
        step_id = str(step.get("id") or "current-step")
        text = _step_text(step)
        operation_type = str(step.get("operation_type") or "none")
        risk_level = str(step.get("risk_level") or "low")
        phase = str(step.get("phase") or "")
        constraints = normalize_user_constraints(self.state)
        blocked: list[str] = []
        roles: list[str] = []
        decision = "do_not_delegate"
        reason = "Step is simple or lacks a specialist delegation need."

        if operation_type in HIGH_RISK_OPERATION_TYPES or _risk_at_least(risk_level, "high"):
            roles.append("safety_reviewer")
            if operation_type in {"schema_change", "data_change", "permission_change", "backup_restore"}:
                roles.append("migration_planner")
            decision = "review_required"
            reason = "High-risk database work requires proposal/review separation and main-agent approval."

        if PERFORMANCE_HINT_RE.search(text):
            roles.append("performance_analyst")
            if decision == "do_not_delegate":
                decision = "delegate"
                reason = "Performance diagnosis benefits from a specialist subagent."

        if SCHEMA_HINT_RE.search(text) or operation_type in {"schema_change", "backup_restore"}:
            roles.append("schema_explorer")
            if decision == "do_not_delegate":
                decision = "delegate"
                reason = "Schema and dependency discovery benefits from a focused read-only subagent."

        if phase == "report" or REPORT_HINT_RE.search(text):
            roles.append("report_writer")
            if decision == "do_not_delegate":
                decision = "delegate"
                reason = "Report assembly benefits from a focused summarization subagent."

        roles = _dedupe(roles)
        if decision in {"delegate", "review_required"}:
            parallel_roles = [role for role in roles if self._roles.get(role, {}).get("can_run_in_parallel")]
            read_only = operation_type not in HIGH_RISK_OPERATION_TYPES and not _risk_at_least(risk_level, "high")
            if len(parallel_roles) > 1 and read_only:
                decision = "parallel_delegate"
                reason = "Multiple read-only specialist tasks can run independently under resource limits."

        if any("只读" in item or "read-only" in item.lower() or "read only" in item.lower() for item in constraints):
            blocked.append("write_operations_forbidden_by_user_constraint")
        if not roles:
            blocked.append("no_matching_specialist_role")

        return {
            "id": _new_id("delegation-policy"),
            "step_id": step_id,
            "decision": decision,  # type: ignore[typeddict-item]
            "selected_roles": roles,
            "reason": reason,
            "constraints": constraints,
            "blocked_reasons": blocked if decision == "do_not_delegate" else [],
            "created_at": _now_iso(),
        }

    def context_packet(self, step: TaskStep, role: AgentRoleDefinition) -> dict[str, Any]:
        intent = self.state.get("current_intent") or {}
        working_set = self.state.get("db_working_set") or {}
        observations = [
            {
                "id": item.get("id"),
                "type": item.get("type"),
                "step_id": item.get("step_id"),
                "summary": item.get("summary"),
            }
            for item in self.state.get("db_observations", [])[-8:]
            if item.get("step_id") in {step.get("id"), None} or item.get("id") in step.get("evidence_required", [])
        ]
        return {
            "task_goal": intent.get("goal") or intent.get("primary_intent") or step.get("description"),
            "parent_step": {
                "id": step.get("id"),
                "phase": step.get("phase"),
                "operation_type": step.get("operation_type"),
                "risk_level": step.get("risk_level"),
                "description": step.get("description"),
                "success_criteria": step.get("success_criteria", []),
                "evidence_required": step.get("evidence_required", []),
            },
            "target_scope": {
                "environment": intent.get("target_environment"),
                "database": intent.get("target_database"),
                "target_objects": intent.get("target_objects", []),
                "schemas": working_set.get("schemas", []),
                "tables": working_set.get("tables", []),
            },
            "user_constraints": normalize_user_constraints(self.state),
            "recent_observations": observations,
            "role_reminder": {
                "name": role["name"],
                "permission_mode": role["permission_mode"],
                "responsibilities": role["responsibilities"],
            },
        }

    def delegated_task(self, decision: DelegationPolicyDecision, role_name: str, step: TaskStep | None = None) -> DelegatedTask:
        step = step or current_step(self.state) or {}
        role = self._roles[role_name]
        plan = self.state.get("db_task_plan") or {}
        intent = self.state.get("current_intent") or {}
        scope = {
            "environment": intent.get("target_environment") or "unknown",
            "database": intent.get("target_database"),
            "target_objects": intent.get("target_objects", []),
            "step_id": step.get("id"),
        }
        forbidden = _dedupe([
            *role.get("disallowed_tools", []),
            "DML",
            "DDL",
            "permission changes",
            "shell database clients",
            "scope expansion",
        ])
        return {
            "id": _new_id("delegated-task"),
            "parent_task_id": str(plan.get("id") or intent.get("id") or "current-task"),
            "parent_step_id": str(step.get("id") or decision.get("step_id") or "current-step"),
            "agent_role": role_name,
            "objective": str(step.get("description") or intent.get("goal") or role["description"]),
            "scope": scope,
            "context_packet": self.context_packet(step, role),  # type: ignore[arg-type]
            "allowed_tools": list(role.get("allowed_tools", [])),
            "forbidden_actions": forbidden,
            "expected_output": role.get("output_schema", "DelegationResult"),
            "success_criteria": list(step.get("success_criteria", []) or role.get("responsibilities", [])),
            "required_evidence": list(step.get("evidence_required", [])),
            "risk_level": str(step.get("risk_level") or "low"),  # type: ignore[typeddict-item]
            "max_turns": int(role.get("max_turns", 2)),
            "max_tool_calls": int(role.get("max_tool_calls", 0)),
            "status": "pending",
            "created_at": _now_iso(),
        }

    def delegated_tasks_for_decision(self, decision: DelegationPolicyDecision, step: TaskStep | None = None) -> list[DelegatedTask]:
        if decision["decision"] == "do_not_delegate":
            return []
        return [
            self.delegated_task(decision, role_name, step=step)
            for role_name in decision.get("selected_roles", [])
            if role_name in self._roles
        ]

    def team_run(self, delegated_tasks: list[DelegatedTask]) -> AgentTeamRun | None:
        if not delegated_tasks:
            return None
        parent_task_id = delegated_tasks[0]["parent_task_id"]
        parallel_count = sum(1 for task in delegated_tasks if self._roles.get(task["agent_role"], {}).get("can_run_in_parallel"))
        return {
            "id": _new_id("agent-team"),
            "parent_task_id": parent_task_id,
            "coordinator_agent_id": str(self.state.get("session_id") or "main-agent"),
            "delegated_task_ids": [task["id"] for task in delegated_tasks],
            "active_agent_ids": [],
            "status": "planning",
            "concurrency_limit": max(1, min(parallel_count or 1, 3)),
            "started_at": _now_iso(),
            "completed_at": None,
            "summary": f"Prepared {len(delegated_tasks)} delegated PostgreSQL subtask(s).",
        }

    def planning_update(self, step: TaskStep | None = None) -> dict[str, Any]:
        """Return state update for roles, delegation decision, tasks, and team run."""
        update: dict[str, Any] = {}
        update.update(self.roles_update())
        decision = self.policy_decision(step)
        tasks = self.delegated_tasks_for_decision(decision, step=step)
        team = self.team_run(tasks)
        update["delegation_policy_decisions"] = [decision]
        update["delegated_tasks"] = tasks
        if team:
            update["agent_team_runs"] = [team]
        update.update(StateManager({**self.state, **update}).metadata_update(last_transition="delegation_planned"))
        return update

    def allowed_tool_specs_for_role(
        self,
        role_name: str,
        specs: list[RegisteredToolSpec] | None = None,
    ) -> list[RegisteredToolSpec]:
        role = self._roles.get(role_name)
        if not role:
            return []
        specs = specs if specs is not None else self.state.get("available_tool_specs", [])
        allowed_names = set(role.get("allowed_tools", []))
        disallowed = set(role.get("disallowed_tools", []))
        allowed_specs: list[RegisteredToolSpec] = []
        for spec in specs:
            capability = spec.get("capability", {})
            name = str(spec.get("name") or "")
            if name not in allowed_names:
                continue
            if name in disallowed:
                continue
            if role["permission_mode"] in {"read_only", "review_only"} and not capability.get("read_only", False):
                continue
            allowed_specs.append(spec)
        return allowed_specs

    def start_record(self, task: DelegatedTask) -> DelegationRecord:
        return {
            "id": _new_id("delegation-record"),
            "delegated_task_id": task["id"],
            "agent_id": _new_id(task["agent_role"]),
            "agent_role": task["agent_role"],
            "status": "started",
            "tool_invocation_refs": [],
            "evidence_refs": [],
            "started_at": _now_iso(),
            "completed_at": None,
            "summary": f"Started {task['agent_role']} for {task['parent_step_id']}.",
        }

    def result_from_task(
        self,
        task: DelegatedTask,
        *,
        agent_id: str | None = None,
        summary: str | None = None,
        findings: list[dict[str, Any]] | None = None,
        evidence_refs: list[str] | None = None,
        sql_used: list[str] | None = None,
        recommended_actions: list[dict[str, Any]] | None = None,
        confidence: float = 0.7,
        open_questions: list[str] | None = None,
    ) -> DelegationResult:
        evidence_refs = evidence_refs if evidence_refs is not None else [
            str(item.get("id"))
            for item in self.state.get("db_observations", [])
            if item.get("id") and item.get("step_id") == task.get("parent_step_id")
        ]
        findings = findings if findings is not None else [
            {
                "type": "delegated_analysis",
                "summary": summary or f"{task['agent_role']} completed scoped analysis.",
                "evidence_refs": evidence_refs,
            }
        ]
        risk_level = task.get("risk_level", "low")
        requires_review = _risk_at_least(risk_level, "high") or bool(open_questions)
        return {
            "id": _new_id("delegation-result"),
            "delegated_task_id": task["id"],
            "agent_id": agent_id or _new_id(task["agent_role"]),
            "status": "needs_review" if requires_review else "succeeded",
            "summary": summary or f"{task['agent_role']} produced scoped PostgreSQL findings.",
            "findings": findings,
            "evidence_refs": evidence_refs,
            "sql_used": sql_used or [],
            "recommended_actions": recommended_actions or [],
            "risk_level": risk_level,
            "confidence": max(0.0, min(float(confidence), 1.0)),
            "open_questions": open_questions or [],
            "requires_human_review": requires_review,
            "created_at": _now_iso(),
        }  # type: ignore[typeddict-item]

    def failure(
        self,
        task: DelegatedTask,
        *,
        failure_type: str,
        message: str,
        recoverable: bool = True,
        suggested_action: str = "retry",
    ) -> DelegationFailure:
        return {
            "id": _new_id("delegation-failure"),
            "delegated_task_id": task["id"],
            "agent_role": task["agent_role"],
            "failure_type": failure_type,  # type: ignore[typeddict-item]
            "message": message,
            "recoverable": recoverable,
            "suggested_action": suggested_action,  # type: ignore[typeddict-item]
            "created_at": _now_iso(),
        }

    def evaluate_result(self, result: DelegationResult, task: DelegatedTask | None = None) -> DelegationEvaluation:
        task = task or next((item for item in self.state.get("delegated_tasks", []) if item.get("id") == result.get("delegated_task_id")), None)
        required_evidence = list((task or {}).get("required_evidence", []) or [])
        evidence_refs = list(result.get("evidence_refs", []) or [])
        sql_used = list(result.get("sql_used", []) or [])
        failed: list[str] = []
        checks: list[dict[str, Any]] = []

        schema_complete = bool(result.get("summary")) and isinstance(result.get("findings"), list)
        checks.append({"name": "output_schema_complete", "passed": schema_complete})
        if not schema_complete:
            failed.append("output_schema_complete")

        evidence_completeness = 1.0
        if required_evidence:
            evidence_completeness = min(1.0, len(evidence_refs) / len(required_evidence))
        evidence_ok = bool(evidence_refs) or not required_evidence
        checks.append({"name": "evidence_available", "passed": evidence_ok, "score": evidence_completeness})
        if not evidence_ok:
            failed.append("evidence_available")

        forbidden_sql = [
            sql for sql in sql_used
            if re.search(r"\b(insert|update|delete|drop|alter|grant|revoke|truncate|vacuum|reindex)\b", sql, re.IGNORECASE)
        ]
        safety_compliant = not forbidden_sql
        checks.append({"name": "read_only_sql_only", "passed": safety_compliant})
        if not safety_compliant:
            failed.append("read_only_sql_only")

        conclusion_supported = bool(result.get("findings")) and (bool(evidence_refs) or not required_evidence)
        checks.append({"name": "conclusion_supported", "passed": conclusion_supported})
        if not conclusion_supported:
            failed.append("conclusion_supported")

        scope_ok = True
        task_scope = (task or {}).get("scope", {})
        for action in result.get("recommended_actions", []):
            target = action.get("target") if isinstance(action, dict) else None
            allowed_targets = [item.get("name") for item in task_scope.get("target_objects", []) if isinstance(item, dict)]
            if target and allowed_targets and target not in allowed_targets:
                scope_ok = False
        checks.append({"name": "scope_respected", "passed": scope_ok})
        if not scope_ok:
            failed.append("scope_respected")

        needs_review = result.get("requires_human_review") or _risk_at_least(result.get("risk_level"), "high")
        status = "failed" if failed else ("needs_review" if needs_review else "passed")
        notes = []
        if needs_review:
            notes.append("High-risk or ambiguous delegated result requires coordinator or human review.")
        return {
            "id": _new_id("delegation-eval"),
            "delegated_task_id": result["delegated_task_id"],
            "result_id": result["id"],
            "status": status,  # type: ignore[typeddict-item]
            "checks": checks,
            "failed_checks": failed,
            "evidence_completeness": round(evidence_completeness, 3),
            "conclusion_supported": conclusion_supported,
            "safety_compliant": safety_compliant,
            "reviewer_notes": notes,
            "created_at": _now_iso(),
        }
