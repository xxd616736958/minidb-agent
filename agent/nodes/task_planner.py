"""Task planning node — decomposes complex instructions into subtask DAGs.

For requests requiring 3+ logical steps, the planner produces a JSON DAG
of subtasks with dependency information. The main loop then executes them
sequentially, respecting dependencies.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from agent.config import get_settings
from agent.llm_factory import create_llm_for_task
from agent.state import AgentState, DBTaskPlan, TaskStep
from collaboration.manager import CollaborationManager
from memory.schema import build_memory_query
from memory.store import get_memory_store
from models.routing import default_model_profiles, fallback_decision_for_error, finish_invocation_record

logger = logging.getLogger(__name__)

# ── Planning prompt ──────────────────────────────────────────

TASK_PLANNER_SYSTEM_PROMPT = """\
You are the planning layer for a PostgreSQL management agent. Your job is to
turn structured task understanding into a safe, auditable task plan.

## Instructions

1. For PostgreSQL work, prefer observe -> diagnose -> propose -> approve ->
   execute -> verify -> report. Never jump directly to mutation.
2. For documentation/general tasks, keep the plan lightweight.
3. Use the suggested workflow as the safety skeleton. You may add task-specific
   details but must not skip required safety phases.
4. If a step mutates schema/data/permissions, mark it requires_approval=true and
   tool_policy="write_tools_after_approval".
5. If a step is read-only, mark tool_policy="read_only_tools".
6. Every step must include success_criteria.
7. Output a JSON array only.

## Task Object Schema
Each task must have:
```json
{
  "id": "short-unique-id",
  "description": "Clear, actionable description of what to do",
  "dependencies": ["id-of-task-that-must-complete-first"],
  "status": "pending",
  "phase": "clarify|observe|diagnose|propose|approve|execute|verify|report",
  "operation_type": "read_only|diagnostic|schema_change|data_change|permission_change|backup_restore|documentation|none",
  "risk_level": "low|medium|high|critical",
  "requires_approval": false,
  "requires_rollback_plan": false,
  "evidence_required": ["string"],
  "success_criteria": ["string"],
  "expected_tools": ["string"],
  "tool_policy": "no_tools|read_only_tools|write_tools_after_approval"
}
```

## Dependency Rules
- A task with empty `dependencies` array can run immediately.
- A task with dependencies can only start after ALL its dependencies are "completed".
- The DAG must be acyclic — no circular dependencies.
- Tasks should be ordered logically: observe → diagnose → propose → approve → execute → verify → report.
- An execute step must depend on an approve step for high/critical changes.
- A write step must not appear in read-only constrained plans.

## Examples

**Simple non-database request**: "What files are in the current directory?"
```json
[]
```

**PostgreSQL performance request**: "This orders query is slow"
```json
[
  {
    "id": "collect-evidence",
    "description": "Collect read-only evidence: EXPLAIN plan, schema, indexes, and row statistics for the slow query",
    "dependencies": [],
    "status": "pending",
    "phase": "observe",
    "operation_type": "diagnostic",
    "risk_level": "low",
    "requires_approval": false,
    "requires_rollback_plan": false,
    "evidence_required": ["execution_plan", "schema_summary", "index_summary"],
    "success_criteria": ["EXPLAIN output is available", "Relevant indexes are listed"],
    "expected_tools": ["postgres_read"],
    "tool_policy": "read_only_tools"
  },
  {
    "id": "diagnose-bottleneck",
    "description": "Analyze evidence to identify the likely bottleneck and optimization options",
    "dependencies": ["collect-evidence"],
    "status": "pending",
    "phase": "diagnose",
    "operation_type": "diagnostic",
    "risk_level": "low",
    "requires_approval": false,
    "requires_rollback_plan": false,
    "evidence_required": ["execution_plan"],
    "success_criteria": ["Bottleneck is explained with evidence"],
    "expected_tools": [],
    "tool_policy": "no_tools"
  },
  {
    "id": "report-findings",
    "description": "Report findings, risks, and recommended next steps without executing changes",
    "dependencies": ["diagnose-bottleneck"],
    "status": "pending",
    "phase": "report",
    "operation_type": "documentation",
    "risk_level": "low",
    "requires_approval": false,
    "requires_rollback_plan": false,
    "evidence_required": [],
    "success_criteria": ["Report includes evidence, cause, recommendation, and risk"],
    "expected_tools": [],
    "tool_policy": "no_tools"
  }
]
```

## Current Context
{memory_context}

## Structured Task Understanding
{intent_context}

## Suggested Workflow
{workflow_context}

Analyze the user's request below together with the structured understanding.
Output ONLY the JSON array (no explanation).
"""


PHASES = {"clarify", "observe", "diagnose", "propose", "approve", "execute", "verify", "report"}
OPERATION_TYPES = {
    "read_only",
    "diagnostic",
    "schema_change",
    "data_change",
    "permission_change",
    "backup_restore",
    "documentation",
    "none",
}
RISK_LEVELS = {"low", "medium", "high", "critical"}
TOOL_POLICIES = {"no_tools", "read_only_tools", "write_tools_after_approval"}
RISK_RANK = {"low": 1, "medium": 2, "high": 3, "critical": 4}
WRITE_OPERATION_TYPES = {"schema_change", "data_change", "permission_change", "backup_restore"}


# ── Node implementation ──────────────────────────────────────

def _should_plan(state: AgentState) -> bool:
    """Determine if planning is needed for this turn."""
    # If we already have a plan and tasks remaining, don't replan
    task_stack = state.get("task_stack", [])
    if task_stack:
        pending = [t for t in task_stack if t.get("status") in ("pending", "running")]
        if pending:
            return False  # Still executing existing plan

    # New user turns are analyzed by intent_analyzer before this node.
    # Plan whenever there is no active task, including after clarification.
    return True


def _parse_plan_output(raw: str) -> list[dict[str, Any]]:
    """Safely parse the LLM's JSON output."""
    # Try direct JSON parse
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # Try extracting JSON from markdown code block
    match = re.search(r'```(?:json)?\s*([\s\S]*?)\s*```', raw)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass

    # Try finding array in the text
    match = re.search(r'\[[\s\S]*\]', raw)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass

    logger.warning(f"Failed to parse plan from LLM output: {raw[:200]}")
    return []


def _repair_plan_output(
    raw: str,
    state: AgentState,
    model_update: dict[str, Any],
) -> list[dict[str, Any]]:
    """Repair malformed structured planner output with the same low-temperature route."""
    try:
        llm, route, record, profile = create_llm_for_task(
            "planning",
            state,
            structured_output_schema="TaskStep[]",
        )
        started_at = time.monotonic()
        response = llm.invoke([
            SystemMessage(
                content=(
                    "Convert the malformed planner output into a valid JSON array of task objects. "
                    "Preserve the task meaning, do not add explanations, and output JSON only."
                )
            ),
            HumanMessage(content=raw),
        ])
        repaired_raw = str(response.content) if hasattr(response, "content") else str(response)
        model_update.setdefault("model_routes", []).append(route)
        model_update.setdefault("model_invocation_policies", []).append(route["policy"])
        model_update.setdefault("model_invocation_records", []).append(
            finish_invocation_record(
                record,
                status="succeeded",
                started_at=started_at,
                output_text=repaired_raw,
                profile=profile,
            )
        )
        return _parse_plan_output(repaired_raw)
    except Exception as e:
        logger.warning(f"Structured plan repair failed: {e}")
        if "route" in locals() and "record" in locals() and "started_at" in locals():
            model_update.setdefault("model_routes", []).append(route)
            model_update.setdefault("model_invocation_policies", []).append(route["policy"])
            model_update.setdefault("model_invocation_records", []).append(
                finish_invocation_record(
                    record,
                    status="failed",
                    started_at=started_at,
                    error=e,
                    profile=locals().get("profile"),
                )
            )
            model_update.setdefault("model_fallback_decisions", []).append(
                fallback_decision_for_error(route, record, e)
            )
        return []


def _build_intent_context(state: AgentState) -> str:
    """Format structured task intent for the planner prompt."""
    intent = state.get("current_intent")
    if not intent:
        return "No structured intent is available."

    fields = {
        "domain": intent.get("domain"),
        "primary_intent": intent.get("primary_intent"),
        "candidate_intents": intent.get("candidate_intents", []),
        "goal": intent.get("goal"),
        "operation_nature": intent.get("operation_nature"),
        "target_environment": intent.get("target_environment"),
        "target_database": intent.get("target_database"),
        "target_objects": intent.get("target_objects", []),
        "risk_level": intent.get("risk_level"),
        "requires_approval": intent.get("requires_approval"),
        "requires_rollback_plan": intent.get("requires_rollback_plan"),
        "evidence_needed": intent.get("evidence_needed", []),
        "constraints": intent.get("constraints", []),
        "output_contract": intent.get("output_contract", {}),
        "next_action": intent.get("next_action"),
    }
    return json.dumps(fields, ensure_ascii=False, indent=2)


def _build_workflow_context(state: AgentState) -> str:
    """Format selected workflow steps for the planner prompt."""
    workflow = state.get("selected_workflow") or "none"
    confirmed = state.get("confirmed_context") or {}
    steps = confirmed.get("workflow_steps", [])
    if not steps:
        return f"selected_workflow: {workflow}\nNo workflow steps are available."
    lines = [f"selected_workflow: {workflow}"]
    for idx, step in enumerate(steps, start=1):
        lines.append(f"{idx}. {step}")
    return "\n".join(lines)


def _render_planner_prompt(state: AgentState) -> str:
    """Render the planner prompt without interpreting JSON examples as format fields."""
    return (
        TASK_PLANNER_SYSTEM_PROMPT
        .replace("{memory_context}", _build_memory_context(state))
        .replace("{intent_context}", _build_intent_context(state))
        .replace("{workflow_context}", _build_workflow_context(state))
    )


def _build_memory_context(state: AgentState) -> str:
    """Format working memory and gated long-term memories for planning."""
    parts = []
    working_memory = state.get("working_memory", {})
    if working_memory:
        parts.append(json.dumps(working_memory, ensure_ascii=False, indent=2))

    memories = state.get("retrieved_memories")
    if memories is None:
        memories = get_memory_store().search(build_memory_query(state), limit=6)
    if memories:
        parts.append("Relevant long-term memories:")
        for memory in memories[:6]:
            parts.append(
                f"- [{memory.get('kind')}/{memory.get('scope')}] {memory.get('summary')}"
            )

    return "\n".join(parts) if parts else "No relevant memory is available."


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slug(text: str, fallback: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", text.lower()).strip("-")
    return slug[:32] or fallback


def _as_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if item is not None]
    return [str(value)]


def _enum(value: Any, valid: set[str], default: str) -> str:
    text = str(value or "").strip()
    return text if text in valid else default


def _coerce_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        return value.lower() in {"true", "yes", "1"}
    return bool(value)


def _intent_risk(intent: dict[str, Any] | None) -> str:
    risk = str((intent or {}).get("risk_level") or "low")
    return risk if risk in RISK_LEVELS else "low"


def _is_write_intent(intent: dict[str, Any] | None) -> bool:
    if not intent:
        return False
    return bool(intent.get("requires_approval")) or str(intent.get("operation_nature") or "") in WRITE_OPERATION_TYPES


def _should_force_top_query_path(intent: dict[str, Any] | None, goal_text: str) -> bool:
    if not _is_top_query_request(goal_text):
        return False
    if _is_write_intent(intent):
        return False
    primary = str((intent or {}).get("primary_intent") or "")
    operation = str((intent or {}).get("operation_nature") or "")
    workflow = str((intent or {}).get("suggested_workflow") or "")
    return (
        primary == "performance_diagnosis"
        or workflow == "performance_diagnosis_workflow"
        or operation in {"diagnostic", "read_only", "unknown", ""}
    )


def _workflow_default_steps(state: AgentState) -> list[dict[str, Any]]:
    """Deterministic fallback steps when the planner LLM returns no usable plan."""
    intent = state.get("current_intent") or {}
    workflow = state.get("selected_workflow") or intent.get("suggested_workflow") or "general_workflow"
    goal = intent.get("goal") or "Complete the user request"
    risk = _intent_risk(intent)
    evidence = intent.get("evidence_needed") or []
    goal_text = f"{goal} {intent.get('user_language_summary') or ''}".lower()
    is_table_listing = bool(
        re.search(r"\b(tables?|relations?)\b", goal_text, re.IGNORECASE)
        or any(token in goal_text for token in ("有哪些表", "列出表", "查看表", "表清单", "所有表"))
    )
    is_database_listing = bool(
        re.search(r"\bdatabases?\b", goal_text, re.IGNORECASE)
        or any(token in goal_text for token in ("有哪些数据库", "数据库列表", "当前环境存在哪些数据库"))
    )

    def step(
        sid: str,
        description: str,
        phase: str,
        operation_type: str,
        dependencies: list[str] | None = None,
        step_risk: str = "low",
        tool_policy: str = "no_tools",
        approval: bool = False,
        rollback: bool = False,
        criteria: list[str] | None = None,
        expected_tools: list[str] | None = None,
        evidence_required: list[str] | None = None,
    ) -> dict[str, Any]:
        return {
            "id": sid,
            "description": description,
            "dependencies": dependencies or [],
            "status": "pending",
            "phase": phase,
            "operation_type": operation_type,
            "risk_level": step_risk,
            "requires_approval": approval,
            "requires_rollback_plan": rollback,
            "evidence_required": evidence_required or [],
            "success_criteria": criteria or ["Step objective is completed and recorded"],
            "expected_tools": expected_tools or [],
            "tool_policy": tool_policy,
        }

    if workflow == "performance_diagnosis_workflow":
        if _should_force_top_query_path(intent, goal_text):
            return _top_query_default_steps(evidence)
        return [
            step(
                "collect-evidence",
                "Collect read-only PostgreSQL evidence for slow queries and current activity",
                "observe",
                "diagnostic",
                step_risk="low",
                tool_policy="read_only_tools",
                expected_tools=["postgres_read", "postgres_top_queries", "postgres_connection_check", "postgres_lock_inspect"],
                evidence_required=evidence or ["top_queries", "active_queries", "connection_info"],
                criteria=["Top or currently active queries are collected, or the unavailable source is clearly explained"],
            ),
            step(
                "diagnose-bottleneck",
                "Analyze the collected evidence and identify likely bottlenecks",
                "diagnose",
                "diagnostic",
                ["collect-evidence"],
                criteria=["Bottleneck is explained with evidence"],
            ),
            step(
                "propose-options",
                "Propose optimization options without executing database changes",
                "propose",
                "diagnostic",
                ["diagnose-bottleneck"],
                step_risk="medium",
                criteria=["Recommendations include benefits, risks, and validation approach"],
            ),
            step(
                "report-findings",
                "Summarize diagnosis, evidence, recommendations, and next approval points",
                "report",
                "documentation",
                ["propose-options"],
                criteria=["Report includes evidence, cause, recommendation, risk, and next steps"],
            ),
        ]

    if workflow == "read_only_analysis_workflow":
        if is_table_listing:
            return [
                step(
                    "list-tables",
                    "List PostgreSQL user tables in the configured target database",
                    "observe",
                    "read_only",
                    step_risk="low",
                    tool_policy="read_only_tools",
                    expected_tools=["postgres_list_schemas", "postgres_list_objects"],
                    evidence_required=evidence or ["schema_summary"],
                    criteria=["User table list is collected from PostgreSQL catalogs or the unavailable source is explained"],
                ),
                step(
                    "report-tables",
                    "Report the table list with schema names and any visibility limits",
                    "report",
                    "documentation",
                    ["list-tables"],
                    criteria=["Answer lists the discovered tables or clearly states that none were found"],
                ),
            ]
        if is_database_listing:
            return [
                step(
                    "list-databases",
                    "List PostgreSQL databases visible to the configured connection",
                    "observe",
                    "read_only",
                    step_risk="low",
                    tool_policy="read_only_tools",
                    expected_tools=["postgres_read"],
                    evidence_required=evidence or ["database_list"],
                    criteria=["Visible databases are collected from PostgreSQL catalogs or the unavailable source is explained"],
                ),
                step(
                    "report-databases",
                    "Report visible databases and the current connected database",
                    "report",
                    "documentation",
                    ["list-databases"],
                    criteria=["Answer lists visible databases and identifies the current database"],
                ),
            ]
        return [
            step(
                "collect-readonly-evidence",
                f"Collect read-only PostgreSQL evidence for: {goal}",
                "observe",
                "diagnostic",
                step_risk="low",
                tool_policy="read_only_tools",
                expected_tools=[
                    "postgres_read",
                    "postgres_connection_check",
                    "postgres_health_check",
                    "postgres_top_queries",
                    "postgres_lock_inspect",
                    "postgres_list_schemas",
                ],
                evidence_required=evidence or ["connection_info", "health_check", "schema_summary"],
                criteria=["Requested read-only database evidence is available, or unavailable checks are explained"],
            ),
            step(
                "analyze-readonly-evidence",
                "Analyze the collected PostgreSQL evidence and identify notable findings",
                "diagnose",
                "diagnostic",
                ["collect-readonly-evidence"],
                criteria=["Findings are explained with evidence and limitations"],
            ),
            step(
                "report-readonly-findings",
                "Report read-only findings, risks, and recommended next steps",
                "report",
                "documentation",
                ["analyze-readonly-evidence"],
                criteria=["Report includes evidence, assumptions, limitations, and next steps"],
            ),
        ]

    if workflow in {"schema_change_workflow", "data_change_workflow", "permission_admin_workflow", "backup_restore_workflow"}:
        operation = {
            "schema_change_workflow": "schema_change",
            "data_change_workflow": "data_change",
            "permission_admin_workflow": "permission_change",
            "backup_restore_workflow": "backup_restore",
        }[workflow]
        return [
            step(
                "observe-current-state",
                "Collect read-only current state and estimate impact before any change",
                "observe",
                "diagnostic",
                step_risk="low",
                tool_policy="read_only_tools",
                expected_tools=["postgres_read"],
                evidence_required=evidence or ["current_state", "impact_estimate"],
                criteria=["Current state and impact estimate are available"],
            ),
            step(
                "draft-change-plan",
                "Draft the change SQL and rollback or recovery plan without executing it",
                "propose",
                operation,
                ["observe-current-state"],
                step_risk=max(risk, "high", key=lambda value: RISK_RANK[value]),
                rollback=True,
                criteria=["Change SQL is drafted", "Rollback or recovery plan is documented"],
            ),
            step(
                "request-approval",
                "Request explicit user approval for the proposed database change",
                "approve",
                operation,
                ["draft-change-plan"],
                step_risk=max(risk, "high", key=lambda value: RISK_RANK[value]),
                approval=True,
                rollback=True,
                criteria=["User has approved the exact change and rollback plan"],
            ),
            step(
                "execute-approved-change",
                "Execute only the user-approved database change",
                "execute",
                operation,
                ["request-approval"],
                step_risk=max(risk, "high", key=lambda value: RISK_RANK[value]),
                tool_policy="write_tools_after_approval",
                approval=True,
                rollback=True,
                expected_tools=["postgres_write"],
                criteria=["Approved SQL executed successfully", "Actual impact is recorded"],
            ),
            step(
                "verify-change",
                "Verify the database state and summarize the result",
                "verify",
                "diagnostic",
                ["execute-approved-change"],
                step_risk="low",
                tool_policy="read_only_tools",
                expected_tools=["postgres_read"],
                criteria=["Post-change state matches expected result"],
            ),
        ]

    if workflow == "documentation_workflow":
        return [
            step(
                "confirm-document-scope",
                f"Confirm document scope and evidence needs for: {goal}",
                "clarify",
                "documentation",
                criteria=["Document scope, audience, and format are clear"],
            ),
            step(
                "collect-supporting-context",
                "Collect any required read-only context or existing notes",
                "observe",
                "read_only",
                ["confirm-document-scope"],
                tool_policy="read_only_tools",
                criteria=["Required source context is available or marked unavailable"],
            ),
            step(
                "draft-document",
                "Draft the requested document using collected context",
                "report",
                "documentation",
                ["collect-supporting-context"],
                criteria=["Document covers requested sections and cites available evidence"],
            ),
        ]

    return [
        step(
            "understand-request",
            f"Confirm the task goal and constraints for: {goal}",
            "clarify",
            "none",
            criteria=["Task goal and constraints are clear"],
        ),
        step(
            "complete-request",
            "Complete the requested work using the appropriate tools and summarize the result",
            "execute",
            "none",
            ["understand-request"],
            criteria=["User request is completed or a clear blocker is reported"],
        ),
    ]


def _is_top_query_request(goal_text: str) -> bool:
    return bool(
        re.search(r"\b(slowest|slow\s+sql|top\s+queries|top\s+query|pg_stat_statements)\b", goal_text, re.IGNORECASE)
        or any(
            token in goal_text
            for token in (
                "最慢",
                "慢sql",
                "慢 sql",
                "慢查询",
                "执行较慢",
                "执行过的最慢",
                "最需要优化的sql",
                "最需要优化的 sql",
                "需要优化的sql",
                "需要优化的 sql",
            )
        )
    )


def _top_query_default_steps(evidence: list[str] | None = None) -> list[dict[str, Any]]:
    evidence = [
        item
        for item in list(evidence or [])
        if item in {"top_queries", "active_queries", "connection_info"}
    ]
    return [
        {
            "id": "collect-top-queries",
            "description": "Collect read-only PostgreSQL top query evidence from configured statistics and current activity",
            "dependencies": [],
            "status": "pending",
            "phase": "observe",
            "operation_type": "diagnostic",
            "risk_level": "low",
            "requires_approval": False,
            "requires_rollback_plan": False,
            "evidence_required": list(dict.fromkeys([*evidence, "top_queries", "active_queries", "connection_info"])),
            "success_criteria": [
                "Top queries are collected from available PostgreSQL statistics, or the unavailable source is clearly explained"
            ],
            "expected_tools": ["postgres_connection_check", "postgres_top_queries", "postgres_lock_inspect"],
            "tool_policy": "read_only_tools",
        },
        {
            "id": "report-optimization-targets",
            "description": "Rank the queries that most need optimization and explain the evidence, limits, and next steps",
            "dependencies": ["collect-top-queries"],
            "status": "pending",
            "phase": "report",
            "operation_type": "documentation",
            "risk_level": "low",
            "requires_approval": False,
            "requires_rollback_plan": False,
            "evidence_required": [],
            "success_criteria": ["Answer identifies the highest-priority SQL candidates with evidence and caveats"],
            "expected_tools": [],
            "tool_policy": "no_tools",
        },
    ]


def _normalize_task(raw: dict[str, Any], index: int, intent: dict[str, Any] | None) -> TaskStep:
    operation = _enum(raw.get("operation_type"), OPERATION_TYPES, "none")
    phase = _enum(raw.get("phase"), PHASES, "execute" if operation in WRITE_OPERATION_TYPES else "observe")
    risk = _enum(raw.get("risk_level"), RISK_LEVELS, _intent_risk(intent))

    if operation in WRITE_OPERATION_TYPES:
        risk = max(risk, "high", key=lambda value: RISK_RANK[value])

    tool_policy_default = "no_tools"
    if phase == "observe" or operation in {"read_only", "diagnostic"}:
        tool_policy_default = "read_only_tools"
    if operation in WRITE_OPERATION_TYPES and phase == "execute":
        tool_policy_default = "write_tools_after_approval"

    task_id = str(raw.get("id") or _slug(str(raw.get("description", "")), f"task-{index}"))
    criteria = _as_str_list(raw.get("success_criteria"))
    if not criteria:
        criteria = ["Step objective is completed and result is recorded"]

    return TaskStep(
        id=task_id,
        description=str(raw.get("description") or f"Task {index + 1}"),
        status=_enum(raw.get("status"), {"pending", "running", "completed", "failed", "skipped"}, "pending"),
        dependencies=_as_str_list(raw.get("dependencies")),
        result=None,
        error=None,
        phase=phase,
        operation_type=operation,
        risk_level=risk,
        requires_approval=_coerce_bool(raw.get("requires_approval"), risk in {"high", "critical"}),
        requires_rollback_plan=_coerce_bool(raw.get("requires_rollback_plan"), operation in WRITE_OPERATION_TYPES),
        evidence_required=_as_str_list(raw.get("evidence_required")),
        success_criteria=criteria,
        expected_tools=_as_str_list(raw.get("expected_tools")),
        tool_policy=_enum(raw.get("tool_policy"), TOOL_POLICIES, tool_policy_default),
    )


def _ensure_unique_ids(tasks: list[TaskStep]) -> None:
    seen: dict[str, int] = {}
    for task in tasks:
        original = task["id"]
        count = seen.get(original, 0)
        if count:
            task["id"] = f"{original}-{count + 1}"
        seen[original] = count + 1


def _fix_dependencies(tasks: list[TaskStep]) -> None:
    valid_ids = {task["id"] for task in tasks}
    for task in tasks:
        task["dependencies"] = [dep for dep in task.get("dependencies", []) if dep in valid_ids and dep != task["id"]]


def _has_phase(tasks: list[TaskStep], phase: str) -> bool:
    return any(task.get("phase") == phase for task in tasks)


def _has_write_step(tasks: list[TaskStep]) -> bool:
    return any(task.get("operation_type") in WRITE_OPERATION_TYPES for task in tasks)


def _insert_approval_before_execute(tasks: list[TaskStep], intent: dict[str, Any] | None) -> None:
    if not _has_write_step(tasks) or _has_phase(tasks, "approve"):
        return

    write_tasks = [task for task in tasks if task.get("operation_type") in WRITE_OPERATION_TYPES]
    deps = list({dep for task in write_tasks for dep in task.get("dependencies", [])})
    approval_id = "request-approval"
    existing_ids = {task["id"] for task in tasks}
    if approval_id in existing_ids:
        approval_id = f"request-approval-{uuid.uuid4().hex[:4]}"

    operation = write_tasks[0].get("operation_type", "data_change")
    approval = TaskStep(
        id=approval_id,
        description="Request explicit user approval for the proposed PostgreSQL change and rollback plan",
        status="pending",
        dependencies=deps,
        result=None,
        error=None,
        phase="approve",
        operation_type=operation,
        risk_level=max(_intent_risk(intent), "high", key=lambda value: RISK_RANK[value]),
        requires_approval=True,
        requires_rollback_plan=True,
        evidence_required=[],
        success_criteria=["User approved the exact change and rollback plan"],
        expected_tools=[],
        tool_policy="no_tools",
    )
    tasks.insert(max(0, tasks.index(write_tasks[0])), approval)

    for task in write_tasks:
        if approval_id not in task["dependencies"]:
            task["dependencies"].append(approval_id)


def validate_and_normalize_plan(tasks_raw: list[dict[str, Any]], state: AgentState) -> list[TaskStep]:
    """Normalize LLM plan output and enforce PostgreSQL safety invariants."""
    intent = state.get("current_intent") or {}
    if not tasks_raw:
        tasks_raw = _workflow_default_steps(state)

    tasks = [_normalize_task(task, idx, intent) for idx, task in enumerate(tasks_raw)]
    _ensure_unique_ids(tasks)
    _fix_dependencies(tasks)
    goal_text = f"{intent.get('goal') or ''} {intent.get('user_language_summary') or ''}".lower()
    is_table_listing = bool(
        re.search(r"\b(tables?|relations?)\b", goal_text, re.IGNORECASE)
        or any(token in goal_text for token in ("有哪些表", "列出表", "查看表", "表清单", "所有表"))
    )
    is_slow_sql = _should_force_top_query_path(intent, goal_text)
    if is_slow_sql:
        tasks = [_normalize_task(task, idx, intent) for idx, task in enumerate(_top_query_default_steps(intent.get("evidence_needed")))]
        _ensure_unique_ids(tasks)
        _fix_dependencies(tasks)

    read_only_constrained = any(
        "只读" in constraint.lower() or "read-only" in constraint.lower() or "read only" in constraint.lower()
        for constraint in intent.get("constraints", [])
    )

    for task in tasks:
        operation = task.get("operation_type", "none")
        phase = task.get("phase", "observe")
        if operation in WRITE_OPERATION_TYPES:
            task["risk_level"] = max(task.get("risk_level", "low"), "high", key=lambda value: RISK_RANK[value])
            task["requires_approval"] = True
            task["requires_rollback_plan"] = True
            if phase == "execute":
                task["tool_policy"] = "write_tools_after_approval"
        elif phase == "observe" or operation in {"read_only", "diagnostic"}:
            task["tool_policy"] = "read_only_tools"

        if read_only_constrained and task.get("tool_policy") == "write_tools_after_approval":
            task["status"] = "skipped"
            task["error"] = "Skipped because the user constrained the task to read-only work."

        if not task.get("success_criteria"):
            task["success_criteria"] = ["Step objective is completed and result is recorded"]

    if is_table_listing:
        observe = next((task for task in tasks if task.get("phase") == "observe"), tasks[0] if tasks else None)
        if observe:
            expected = list(dict.fromkeys([*observe.get("expected_tools", []), "postgres_list_schemas", "postgres_list_objects"]))
            criteria = list(
                dict.fromkeys(
                    [
                        *observe.get("success_criteria", []),
                        "User table list is collected from PostgreSQL catalogs or the unavailable source is explained",
                    ]
                )
            )
            evidence_required = list(dict.fromkeys([*observe.get("evidence_required", []), "schema_summary"]))
            observe["operation_type"] = "read_only"
            observe["tool_policy"] = "read_only_tools"
            observe["expected_tools"] = expected
            observe["success_criteria"] = criteria
            observe["evidence_required"] = evidence_required

    if is_slow_sql:
        observe = next((task for task in tasks if task.get("phase") == "observe"), tasks[0] if tasks else None)
        if observe:
            observe["operation_type"] = "diagnostic"
            observe["tool_policy"] = "read_only_tools"
            observe["expected_tools"] = list(dict.fromkeys([*observe.get("expected_tools", []), "postgres_top_queries"]))
            observe["evidence_required"] = list(dict.fromkeys([*observe.get("evidence_required", []), "top_queries"]))
            observe["success_criteria"] = list(
                dict.fromkeys(
                    [
                        *observe.get("success_criteria", []),
                        "Slowest queries are collected from available PostgreSQL statistics or the unavailable source is explained",
                    ]
                )
            )

    if not read_only_constrained:
        _insert_approval_before_execute(tasks, intent)
        _fix_dependencies(tasks)

    return tasks


def _plan_summary(tasks: list[TaskStep], intent: dict[str, Any] | None) -> str:
    goal = (intent or {}).get("goal") or "Complete the user request"
    return f"{goal} ({len(tasks)} planned step{'s' if len(tasks) != 1 else ''})"


def build_db_task_plan(tasks: list[TaskStep], state: AgentState) -> DBTaskPlan:
    intent = state.get("current_intent") or {}
    risks = [task.get("risk_level", "low") for task in tasks]
    global_risk = max(risks or [_intent_risk(intent)], key=lambda value: RISK_RANK[value])
    requires_confirmation = any(task.get("requires_approval") for task in tasks) or global_risk in {"high", "critical"}
    now = _now_iso()
    status = "awaiting_approval" if requires_confirmation and any(task.get("phase") == "approve" for task in tasks) else "draft"
    return {
        "id": f"plan-{uuid.uuid4().hex[:12]}",
        "intent_id": str(intent.get("id") or ""),
        "workflow": str(state.get("selected_workflow") or intent.get("suggested_workflow") or "general_workflow"),
        "summary": _plan_summary(tasks, intent),
        "status": status,
        "steps": tasks,
        "assumptions": list(intent.get("assumptions", [])),
        "constraints": list(intent.get("constraints", [])),
        "global_risk_level": global_risk,
        "requires_user_confirmation": requires_confirmation,
        "created_at": now,
        "updated_at": now,
    }


def _format_plan(tasks: list[TaskStep], plan: DBTaskPlan) -> str:
    lines = [
        f"## Execution Plan ({len(tasks)} steps)",
        f"- Workflow: {plan['workflow']}",
        f"- Global risk: {plan['global_risk_level']}",
        f"- Requires confirmation: {plan['requires_user_confirmation']}",
        "",
    ]
    for i, task in enumerate(tasks, start=1):
        deps = f" (after: {', '.join(task['dependencies'])})" if task["dependencies"] else ""
        meta = (
            f"phase={task.get('phase', 'n/a')}, "
            f"risk={task.get('risk_level', 'n/a')}, "
            f"policy={task.get('tool_policy', 'n/a')}"
        )
        lines.append(f"{i}. [{task['id']}] {task['description']}{deps}")
        lines.append(f"   - {meta}")
        if task.get("requires_approval"):
            lines.append("   - requires approval")
        if task.get("requires_rollback_plan"):
            lines.append("   - requires rollback plan")
        criteria = task.get("success_criteria", [])
        if criteria:
            lines.append(f"   - success: {criteria[0]}")
    return "\n".join(lines)


def task_planner(state: AgentState) -> dict[str, Any]:
    """Task planning node — optionally decomposes complex instructions.

    Only runs on the first message of a conversation or when
    the previous plan is fully complete.
    """
    if not _should_plan(state):
        logger.debug("Skipping task planner — not needed for this turn")
        return {}

    messages = state.get("messages", [])
    if not messages:
        return {}

    # Find the last user message
    user_msg = None
    for m in reversed(messages):
        if isinstance(m, HumanMessage) or getattr(m, "type", None) == "human":
            user_msg = m
            break

    if not user_msg:
        return {}

    user_content = str(user_msg.content) if hasattr(user_msg, "content") else str(user_msg)
    logger.info(f"Task planner analyzing: {user_content[:100]}...")

    try:
        llm, route, record, profile = create_llm_for_task("planning", state, structured_output_schema="TaskStep[]")
        started_at = time.monotonic()
        response = llm.invoke([
            SystemMessage(content=_render_planner_prompt(state)),
            HumanMessage(content=user_content),
        ])
        model_update = {
            "model_routes": [route],
            "model_invocation_policies": [route["policy"]],
            "model_invocation_records": [
                finish_invocation_record(
                    record,
                    status="succeeded",
                    started_at=started_at,
                    output_text=str(response.content) if hasattr(response, "content") else str(response),
                    profile=profile,
                )
            ],
        }
        if not state.get("model_profiles"):
            model_update["model_profiles"] = default_model_profiles()
    except Exception as e:
        logger.error(f"Task planner LLM call failed: {e}")
        # Non-fatal — agent can work without a plan
        failure_update = {"plan": None, "task_stack": [], "current_task_index": 0}
        if "route" in locals() and "record" in locals() and "started_at" in locals():
            failure_update.update(
                {
                    "model_routes": [route],
                    "model_invocation_policies": [route["policy"]],
                    "model_invocation_records": [
                        finish_invocation_record(record, status="failed", started_at=started_at, error=e, profile=locals().get("profile"))
                    ],
                    "model_fallback_decisions": [fallback_decision_for_error(route, record, e)],
                }
            )
            if not state.get("model_profiles"):
                failure_update["model_profiles"] = default_model_profiles()
        return failure_update

    raw = str(response.content) if hasattr(response, "content") else str(response)
    tasks_raw = _parse_plan_output(raw)

    intent = state.get("current_intent") or {}
    if not tasks_raw and intent.get("domain") in {"postgresql", "documentation"}:
        tasks_raw = _repair_plan_output(raw, state, model_update)

    if not tasks_raw and intent.get("domain") not in {"postgresql", "documentation"}:
        logger.info("Task planner: no decomposition needed (simple request)")
        return {
            "plan": None,
            "task_stack": [],
            "db_task_plan": None,
            "current_task_index": 0,
            **model_update,
        }

    if not tasks_raw:
        logger.info("Task planner: using workflow fallback plan")

    tasks = validate_and_normalize_plan(tasks_raw, state)
    db_task_plan = build_db_task_plan(tasks, state)
    plan_display = _format_plan(tasks, db_task_plan)
    logger.info(f"Task planner created plan: {len(tasks)} steps")

    update = {
        "plan": plan_display,
        "task_stack": tasks,
        "db_task_plan": db_task_plan,
        "plan_history": [db_task_plan],
        "current_task_index": 0,
    }
    update.update(model_update)
    update.update(CollaborationManager(state).plan_review_update(db_task_plan))
    return update
