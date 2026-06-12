"""Context management utilities for PostgreSQL-focused agent execution."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any

from agent.state import (
    AgentState,
    ContextSnapshot,
    DBObservation,
    DBWorkingSet,
    ResultDigest,
    StepContextPacket,
    TaskStep,
)
from memory.schema import build_memory_query
from memory.store import get_memory_store


DEFAULT_CONTEXT_TOKEN_BUDGET = 6000
MAX_CONTEXT_OBSERVATIONS = 6
MAX_CONTEXT_APPROVALS = 4
MAX_CONTEXT_VERIFICATIONS = 6
MAX_SAMPLE_ROWS = 5
SENSITIVE_FIELD_RE = re.compile(
    r"(password|passwd|pwd|token|secret|api[_-]?key|email|phone|ssn|身份证|手机号|邮箱)",
    re.IGNORECASE,
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def estimate_tokens(text: str) -> int:
    return max(len(text.encode("utf-8")) // 4, 1)


def mask_sensitive_value(value: Any) -> Any:
    if value is None:
        return None
    text = str(value)
    if len(text) <= 4:
        return "***"
    return f"{text[:2]}***{text[-2:]}"


def mask_sensitive_row(row: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    masked = {}
    masked_fields: list[str] = []
    for key, value in row.items():
        if SENSITIVE_FIELD_RE.search(str(key)):
            masked[key] = mask_sensitive_value(value)
            masked_fields.append(str(key))
        else:
            masked[key] = value
    return masked, masked_fields


def normalize_user_constraints(state: AgentState) -> list[str]:
    """Collect explicit user constraints from state and current intent."""
    constraints: list[str] = []
    constraints.extend(str(item) for item in state.get("user_constraints", []) if item)

    intent = state.get("current_intent") or {}
    constraints.extend(str(item) for item in intent.get("constraints", []) if item)

    seen: set[str] = set()
    unique: list[str] = []
    for item in constraints:
        if item not in seen:
            seen.add(item)
            unique.append(item)
    return unique


def current_step(state: AgentState) -> TaskStep | None:
    steps = list(state.get("task_stack", []))
    step_id = state.get("current_step_id")
    if step_id:
        for step in steps:
            if step.get("id") == step_id:
                return step

    idx = state.get("current_task_index", 0)
    if steps and idx < len(steps):
        return steps[idx]
    return None


def observation_matches_step(observation: DBObservation, step: TaskStep) -> bool:
    step_id = step.get("id")
    if observation.get("step_id") == step_id:
        return True
    evidence_required = set(step.get("evidence_required", []))
    return observation.get("type") in evidence_required


def build_allowed_actions(policy: str) -> list[str]:
    if policy == "no_tools":
        return ["reason from existing context", "write explanation", "request clarification"]
    if policy == "read_only_tools":
        return ["run read-only SQL", "run EXPLAIN", "inspect schema", "inspect indexes"]
    if policy == "write_tools_after_approval":
        return ["execute only approved SQL", "record affected rows", "prepare verification"]
    return ["reason", "ask user"]


def build_blocked_actions(policy: str, constraints: list[str]) -> list[str]:
    blocked: list[str] = []
    if policy == "no_tools":
        blocked.extend(["calling tools", "executing SQL"])
    elif policy == "read_only_tools":
        blocked.extend(["DDL", "DML", "permission changes", "destructive SQL"])
    elif policy == "write_tools_after_approval":
        blocked.append("executing unapproved SQL")

    if any("只读" in item or "read-only" in item.lower() or "read only" in item.lower() for item in constraints):
        blocked.extend(["all write SQL", "schema/data/permission changes"])
    return sorted(set(blocked))


def build_missing_context(step: TaskStep, observations: list[DBObservation]) -> list[str]:
    available = {obs.get("type") for obs in observations}
    missing = []
    for item in step.get("evidence_required", []):
        if item not in available:
            missing.append(str(item))
    return missing


def build_step_context_packet(state: AgentState) -> StepContextPacket | None:
    """Build the current-step packet used by prompts and policy checks."""
    step = current_step(state)
    if not step:
        return None

    constraints = normalize_user_constraints(state)
    observations = [
        obs for obs in state.get("db_observations", [])
        if observation_matches_step(obs, step)
    ][-MAX_CONTEXT_OBSERVATIONS:]
    approvals = [
        item for item in state.get("approval_decisions", [])
        if item.get("step_id") == step.get("id")
    ][-MAX_CONTEXT_APPROVALS:]
    verifications = [
        item for item in state.get("verification_results", [])
        if item.get("step_id") == step.get("id")
    ][-MAX_CONTEXT_VERIFICATIONS:]

    policy = str(step.get("tool_policy", "no_tools"))
    return {
        "step_id": step["id"],
        "phase": str(step.get("phase", "")),
        "description": step.get("description", ""),
        "risk_level": str(step.get("risk_level", "low")),
        "tool_policy": policy,
        "success_criteria": list(step.get("success_criteria", [])),
        "user_constraints": constraints,
        "relevant_observations": observations,
        "relevant_approvals": approvals,
        "relevant_verifications": verifications,
        "allowed_actions": build_allowed_actions(policy),
        "blocked_actions": build_blocked_actions(policy, constraints),
        "missing_context": build_missing_context(step, observations),
    }


def format_step_context_packet(packet: StepContextPacket | None) -> str:
    if not packet:
        return ""

    lines = [
        "## Current Step Context Packet",
        f"- Step ID: {packet['step_id']}",
        f"- Phase: {packet['phase']}",
        f"- Description: {packet['description']}",
        f"- Risk: {packet['risk_level']}",
        f"- Tool policy: {packet['tool_policy']}",
    ]
    if packet["success_criteria"]:
        lines.append(f"- Success criteria: {', '.join(packet['success_criteria'])}")
    if packet["user_constraints"]:
        lines.append(f"- User constraints: {', '.join(packet['user_constraints'])}")
    if packet["allowed_actions"]:
        lines.append(f"- Allowed actions: {', '.join(packet['allowed_actions'])}")
    if packet["blocked_actions"]:
        lines.append(f"- Blocked actions: {', '.join(packet['blocked_actions'])}")
    if packet["missing_context"]:
        lines.append(f"- Missing context: {', '.join(packet['missing_context'])}")

    observations = packet["relevant_observations"]
    if observations:
        lines.append("\n### Relevant Observations")
        for obs in observations:
            lines.append(f"- [{obs.get('type')}] {obs.get('summary', '')[:300]}")

    approvals = packet["relevant_approvals"]
    if approvals:
        lines.append("\n### Relevant Approvals")
        for approval in approvals:
            lines.append(
                f"- {approval.get('status')} risk={approval.get('risk_level')} "
                f"env={approval.get('target_environment')}"
            )

    verifications = packet["relevant_verifications"]
    if verifications:
        lines.append("\n### Relevant Verification Results")
        for verification in verifications:
            lines.append(f"- {verification.get('status')}: {verification.get('summary')}")

    return "\n".join(lines)


def build_db_working_set(state: AgentState) -> DBWorkingSet | None:
    """Build a lightweight working set from current intent and observations."""
    intent = state.get("current_intent") or {}
    if intent.get("domain") != "postgresql":
        return state.get("db_working_set")

    existing = state.get("db_working_set") or {}
    target_objects = intent.get("target_objects", []) or []
    tables = set(existing.get("tables", []))
    schemas = set(existing.get("schemas", []))
    indexes = dict(existing.get("indexes", {}))
    row_counts = dict(existing.get("row_counts", {}))
    source_observation_ids = list(existing.get("source_observation_ids", []))

    for obj in target_objects:
        name = str(obj.get("name", ""))
        obj_type = str(obj.get("type", ""))
        if obj_type in {"table", "relation"} and name:
            tables.add(name)
        if obj_type == "schema" and name:
            schemas.add(name)
        if obj_type == "index" and name:
            indexes.setdefault("unknown", []).append(name)

    known_queries = list(existing.get("known_queries", []))
    for artifact in intent.get("input_artifacts", []) or []:
        if isinstance(artifact, dict) and artifact.get("type") in {"sql", "query"}:
            known_queries.append(artifact)

    for obs in state.get("db_observations", [])[-MAX_CONTEXT_OBSERVATIONS:]:
        obs_id = obs.get("id")
        if obs_id and obs_id not in source_observation_ids:
            source_observation_ids.append(obs_id)

    return {
        "target_environment": str(intent.get("target_environment") or existing.get("target_environment") or "unknown"),
        "target_database": intent.get("target_database") or existing.get("target_database"),
        "schemas": sorted(schemas),
        "tables": sorted(tables),
        "columns": dict(existing.get("columns", {})),
        "indexes": indexes,
        "known_queries": known_queries[-10:],
        "row_counts": row_counts,
        "statistics_refs": list(existing.get("statistics_refs", [])),
        "last_refreshed_at": now_iso(),
        "source_observation_ids": source_observation_ids[-20:],
        "stale_reason": existing.get("stale_reason"),
    }


def build_result_digest(observation: DBObservation) -> ResultDigest | None:
    """Create a safe digest for tabular observation payloads."""
    payload = observation.get("payload", {})
    rows = payload.get("rows")
    if not isinstance(rows, list):
        return None

    row_count = len(rows)
    sample_rows: list[dict[str, Any]] = []
    masked_fields: set[str] = set()
    for row in rows[:MAX_SAMPLE_ROWS]:
        if isinstance(row, dict):
            masked, fields = mask_sensitive_row(row)
            sample_rows.append(masked)
            masked_fields.update(fields)

    column_names = list(sample_rows[0].keys()) if sample_rows else []
    column_types = {
        key: type(value).__name__
        for key, value in (sample_rows[0].items() if sample_rows else [])
    }
    return {
        "observation_id": observation["id"],
        "row_count": row_count,
        "column_names": column_names,
        "column_types": column_types,
        "sample_rows": sample_rows,
        "aggregates": {},
        "truncation_applied": row_count > MAX_SAMPLE_ROWS,
        "sensitive_fields_masked": sorted(masked_fields),
    }


def sql_hash(sql: str) -> str:
    return hashlib.sha256(sql.encode("utf-8")).hexdigest()[:16]


def build_context_snapshot(state: AgentState) -> ContextSnapshot:
    intent = state.get("current_intent") or {}
    plan = state.get("db_task_plan") or {}
    return {
        "intent_id": str(intent.get("id") or ""),
        "plan_id": str(plan.get("id") or ""),
        "current_step_id": state.get("current_step_id"),
        "user_constraints": normalize_user_constraints(state),
        "observation_ids": [obs["id"] for obs in state.get("db_observations", [])],
        "approval_ids": [item["id"] for item in state.get("approval_decisions", [])],
        "verification_ids": [item["id"] for item in state.get("verification_results", [])],
        "db_working_set_ref": "state.db_working_set" if state.get("db_working_set") else None,
        "replan_trigger": state.get("replan_trigger"),
        "created_at": now_iso(),
    }


def compact_prompt_context(text: str, budget: int = DEFAULT_CONTEXT_TOKEN_BUDGET) -> str:
    """Keep context under a rough token budget by truncating low-priority tail."""
    if estimate_tokens(text) <= budget:
        return text
    max_chars = budget * 4
    return text[:max_chars] + "\n\n[Context truncated by token budget]"


def build_prompt_context(state: AgentState) -> tuple[str, StepContextPacket | None]:
    """Build the structured prompt context used by llm_reason."""
    packet = build_step_context_packet(state)
    sections: list[str] = []

    constraints = normalize_user_constraints(state)
    if constraints:
        sections.append("## User Constraints\n" + "\n".join(f"- {item}" for item in constraints))

    intent = state.get("current_intent")
    if intent:
        sections.append(
            "## Current Task Intent\n"
            + json.dumps(
                {
                    "domain": intent.get("domain"),
                    "primary_intent": intent.get("primary_intent"),
                    "goal": intent.get("goal"),
                    "risk_level": intent.get("risk_level"),
                    "target_environment": intent.get("target_environment"),
                    "requires_approval": intent.get("requires_approval"),
                    "requires_rollback_plan": intent.get("requires_rollback_plan"),
                },
                ensure_ascii=False,
                indent=2,
            )
        )

    working_set = state.get("db_working_set")
    if working_set:
        sections.append(
            "## DB Working Set\n"
            + json.dumps(
                {
                    "environment": working_set.get("target_environment"),
                    "database": working_set.get("target_database"),
                    "schemas": working_set.get("schemas", []),
                    "tables": working_set.get("tables", []),
                    "known_queries_count": len(working_set.get("known_queries", [])),
                    "source_observation_ids": working_set.get("source_observation_ids", []),
                    "stale_reason": working_set.get("stale_reason"),
                },
                ensure_ascii=False,
                indent=2,
            )
        )

    runtime = state.get("db_task_runtime")
    integrity_reports = state.get("state_integrity_reports", [])
    recovery_summary = state.get("recovery_summary")
    if runtime or integrity_reports or recovery_summary:
        latest_integrity = integrity_reports[-1] if integrity_reports else None
        sections.append(
            "## State Management\n"
            + json.dumps(
                {
                    "runtime": runtime,
                    "recovery_summary": recovery_summary,
                    "latest_integrity": {
                        "ok": (latest_integrity or {}).get("ok"),
                        "errors": (latest_integrity or {}).get("errors", []),
                        "warnings": (latest_integrity or {}).get("warnings", []),
                    } if latest_integrity else None,
                },
                ensure_ascii=False,
                indent=2,
            )
        )

    workspace = state.get("workspace_profile")
    db_env = state.get("database_environment")
    runtime_policy = state.get("runtime_policy")
    task_workspace = state.get("task_workspace")
    if workspace or db_env or runtime_policy or task_workspace:
        sections.append(
            "## Execution Environment\n"
            + json.dumps(
                {
                    "workspace_root": (workspace or {}).get("root_path"),
                    "artifact_root": (workspace or {}).get("artifact_root"),
                    "task_workspace": (task_workspace or {}).get("root_path"),
                    "database_environment": {
                        "environment_name": (db_env or {}).get("environment_name"),
                        "target_database": (db_env or {}).get("target_database"),
                        "safe_host_label": (db_env or {}).get("safe_host_label"),
                        "access_mode": (db_env or {}).get("access_mode"),
                        "is_production": (db_env or {}).get("is_production"),
                    },
                    "runtime_policy": {
                        "allow_shell_database_clients": (runtime_policy or {}).get("allow_shell_database_clients"),
                        "allow_database_writes": (runtime_policy or {}).get("allow_database_writes"),
                    },
                },
                ensure_ascii=False,
                indent=2,
            )
        )

    security_decisions = state.get("security_policy_decisions", [])[-5:]
    sql_reports = state.get("sql_safety_reports", [])[-3:]
    output_policy = state.get("output_safety_policy")
    pending_approval = state.get("pending_approval")
    if security_decisions or sql_reports or output_policy or pending_approval:
        sections.append(
            "## Safety Guardrails\n"
            + json.dumps(
                {
                    "pending_approval": {
                        "id": pending_approval.get("id"),
                        "step_id": pending_approval.get("step_id"),
                        "status": pending_approval.get("status"),
                        "risk_level": pending_approval.get("risk_level"),
                        "target_environment": pending_approval.get("target_environment"),
                        "sql_hash": pending_approval.get("sql_hash"),
                    } if pending_approval else None,
                    "recent_decisions": [
                        {
                            "scope": item.get("scope"),
                            "subject": item.get("subject"),
                            "decision": item.get("decision"),
                            "risk_level": item.get("risk_level"),
                            "reasons": item.get("reasons", [])[:2],
                        }
                        for item in security_decisions
                    ],
                    "recent_sql_reports": [
                        {
                            "sql_hash": item.get("sql_hash"),
                            "classification": item.get("classification"),
                            "risk_level": item.get("risk_level"),
                            "requires_approval": item.get("requires_approval"),
                            "denial_reason": item.get("denial_reason"),
                        }
                        for item in sql_reports
                    ],
                    "output_policy": {
                        "max_rows": (output_policy or {}).get("max_rows"),
                        "mask_sensitive_fields": (output_policy or {}).get("mask_sensitive_fields"),
                        "allow_raw_result_in_context": (output_policy or {}).get("allow_raw_result_in_context"),
                    } if output_policy else None,
                },
                ensure_ascii=False,
                indent=2,
            )
        )

    task_card = state.get("task_card")
    plan_review = state.get("plan_review")
    approval_card = state.get("approval_card")
    collaboration_events = state.get("collaboration_events", [])[-8:]
    user_feedback = state.get("user_feedback", [])[-5:]
    if task_card or plan_review or approval_card or collaboration_events or user_feedback:
        sections.append(
            "## Human Collaboration\n"
            + json.dumps(
                {
                    "task_card": {
                        "id": task_card.get("id"),
                        "status": task_card.get("status"),
                        "goal": task_card.get("goal"),
                        "risk_level": task_card.get("risk_level"),
                        "target_environment": task_card.get("target_environment"),
                        "target_database": task_card.get("target_database"),
                        "missing_slots": task_card.get("missing_slots", []),
                        "user_constraints": task_card.get("user_constraints", []),
                    } if task_card else None,
                    "plan_review": {
                        "id": plan_review.get("id"),
                        "plan_id": plan_review.get("plan_id"),
                        "status": plan_review.get("status"),
                        "reviewed_steps": plan_review.get("reviewed_steps", [])[:8],
                        "user_message": plan_review.get("user_message"),
                    } if plan_review else None,
                    "approval_card": {
                        "approval_id": approval_card.get("approval_id"),
                        "step_id": approval_card.get("step_id"),
                        "tool_name": approval_card.get("tool_name"),
                        "risk_level": approval_card.get("risk_level"),
                        "sql_hash": approval_card.get("sql_hash"),
                        "replay_policy": approval_card.get("replay_policy"),
                        "options": approval_card.get("options", []),
                    } if approval_card else None,
                    "recent_events": [
                        {
                            "event_type": event.get("event_type"),
                            "step_id": event.get("step_id"),
                            "summary": event.get("summary"),
                            "payload_ref": event.get("payload_ref"),
                        }
                        for event in collaboration_events
                    ],
                    "recent_user_feedback": [
                        {
                            "feedback_type": item.get("feedback_type"),
                            "target_ref": item.get("target_ref"),
                            "content": item.get("content"),
                            "structured_delta": item.get("structured_delta", {}),
                        }
                        for item in user_feedback
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
        )

    error_records = state.get("error_records", [])[-5:]
    recovery_decisions = state.get("recovery_decisions", [])[-5:]
    recovery_attempts = state.get("recovery_attempts", [])[-5:]
    retry_budgets = state.get("retry_budgets", [])[-5:]
    active_decision = state.get("active_recovery_decision")
    error_reports = state.get("error_reports", [])[-3:]
    if error_records or recovery_decisions or recovery_attempts or retry_budgets or active_decision or error_reports:
        sections.append(
            "## Error Handling and Self-Repair\n"
            + json.dumps(
                {
                    "active_recovery_decision": {
                        "id": active_decision.get("id"),
                        "error_id": active_decision.get("error_id"),
                        "action": active_decision.get("action"),
                        "reason": active_decision.get("reason"),
                        "requires_new_approval": active_decision.get("requires_new_approval"),
                        "next_node": active_decision.get("next_node"),
                    } if active_decision else None,
                    "recent_errors": [
                        {
                            "id": item.get("id"),
                            "source": item.get("source"),
                            "error_type": item.get("error_type"),
                            "severity": item.get("severity"),
                            "step_id": item.get("step_id"),
                            "tool_name": item.get("tool_name"),
                            "sql_hash": item.get("sql_hash"),
                            "sqlstate": item.get("sqlstate"),
                            "retryable": item.get("retryable"),
                            "requires_user_action": item.get("requires_user_action"),
                            "message": item.get("message"),
                        }
                        for item in error_records
                    ],
                    "recent_recovery_decisions": [
                        {
                            "id": item.get("id"),
                            "error_id": item.get("error_id"),
                            "action": item.get("action"),
                            "reason": item.get("reason"),
                            "requires_new_approval": item.get("requires_new_approval"),
                            "next_node": item.get("next_node"),
                        }
                        for item in recovery_decisions
                    ],
                    "recent_recovery_attempts": [
                        {
                            "id": item.get("id"),
                            "error_id": item.get("error_id"),
                            "action": item.get("action"),
                            "status": item.get("status"),
                            "attempt_no": item.get("attempt_no"),
                            "summary": item.get("summary"),
                        }
                        for item in recovery_attempts
                    ],
                    "retry_budgets": retry_budgets,
                    "recent_error_reports": [
                        {
                            "id": item.get("id"),
                            "status": item.get("status"),
                            "step_id": item.get("step_id"),
                            "user_summary": item.get("user_summary"),
                            "next_options": item.get("next_options", []),
                        }
                        for item in error_reports
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
        )

    quality_gates = state.get("quality_gates", [])[-5:]
    evaluation_results = state.get("evaluation_results", [])[-5:]
    replay_cases = state.get("replay_cases", [])[-3:]
    quality_reports = state.get("quality_reports", [])[-3:]
    if quality_gates or evaluation_results or replay_cases or quality_reports:
        sections.append(
            "## Evaluation Testing and Quality Control\n"
            + json.dumps(
                {
                    "recent_quality_gates": [
                        {
                            "id": item.get("id"),
                            "gate_type": item.get("gate_type"),
                            "target_ref": item.get("target_ref"),
                            "status": item.get("status"),
                            "blocking": item.get("blocking"),
                            "failed_checks": item.get("failed_checks", []),
                        }
                        for item in quality_gates
                    ],
                    "recent_evaluation_results": [
                        {
                            "id": item.get("id"),
                            "case_id": item.get("case_id"),
                            "status": item.get("status"),
                            "scores": item.get("scores", {}),
                            "failed_assertions": item.get("failed_assertions", []),
                            "requires_human_review": item.get("requires_human_review"),
                        }
                        for item in evaluation_results
                    ],
                    "recent_replay_cases": [
                        {
                            "id": item.get("id"),
                            "source": item.get("source"),
                            "expected_recovery": item.get("expected_recovery"),
                            "expected_final_status": item.get("expected_final_status"),
                            "sensitivity": item.get("sensitivity"),
                        }
                        for item in replay_cases
                    ],
                    "recent_quality_reports": [
                        {
                            "id": item.get("id"),
                            "scope": item.get("scope"),
                            "status": item.get("status"),
                            "human_review_required": item.get("human_review_required"),
                            "uncovered_risks": item.get("uncovered_risks", []),
                            "recommendations": item.get("recommendations", []),
                        }
                        for item in quality_reports
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
        )

    query = build_memory_query(state)
    memories = state.get("retrieved_memories")
    if memories is None:
        memories = get_memory_store().search(query, limit=5)
    if memories:
        sections.append(
            "## Retrieved Long-Term Memories\n"
            + "\n".join(
                f"- [{item.get('kind')}/{item.get('scope')}] {item.get('summary')}"
                for item in memories[:5]
            )
        )

    packet_text = format_step_context_packet(packet)
    if packet_text:
        sections.append(packet_text)

    budget = int(state.get("context_token_budget") or DEFAULT_CONTEXT_TOKEN_BUDGET)
    return compact_prompt_context("\n\n".join(sections), budget), packet


def retrieve_relevant_memories(state: AgentState, limit: int = 5):
    """Retrieve gated long-term memories for the current task state."""
    return get_memory_store().search(build_memory_query(state), limit=limit)
