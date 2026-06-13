"""Plan-step driven Agent Loop nodes for PostgreSQL management workflows."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from langchain_core.messages import AIMessage, SystemMessage, ToolMessage

from agent.context import (
    build_context_snapshot,
    build_result_digest,
    build_step_context_packet,
    retrieve_relevant_memories,
)
from agent.state import AgentState, DBObservation, ResultDigest, TaskStep, VerificationResult
from collaboration.manager import CollaborationManager
from error_handling.classifier import ErrorClassifier
from error_handling.recovery import RecoveryEngine
from execution.environment import ArtifactStore, ExecutionEnvironmentManager
from memory.consolidator import consolidate_memories
from safety.engine import SecurityPolicyEngine
from state_management.manager import StateManager
from state_management.validator import StateValidator
from tools.policy import evaluate_tool_call_full, make_invocation_record, tool_call_items

logger = logging.getLogger(__name__)

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _plan_steps(state: AgentState) -> list[TaskStep]:
    return StateManager(state).plan_steps()


def _completed_ids(steps: list[TaskStep]) -> set[str]:
    return {
        step["id"]
        for step in steps
        if step.get("status") in {"completed", "skipped"}
    }


def _find_current_index(steps: list[TaskStep], current_step_id: str | None) -> int:
    return StateManager({"current_step_id": current_step_id, "current_task_index": 0}).find_step_index(steps, current_step_id)


def _sync_plan_and_stack(
    state: AgentState,
    steps: list[TaskStep],
    current_idx: int | None = None,
    plan_status: str | None = None,
) -> dict[str, Any]:
    return StateManager(state).sync_plan_and_stack(steps, current_idx, plan_status)


def step_scheduler(state: AgentState) -> dict[str, Any]:
    """Select the next runnable plan step and mark it running."""
    if state.get("error"):
        return {}

    steps = _plan_steps(state)
    if not steps:
        update = {"loop_status": "completed", "current_step_id": None}
        next_state = {**state, **update}
        update.update(StateManager(next_state).runtime_update(last_node="step_scheduler"))
        update.update(StateValidator(next_state).validation_update())
        return update

    completed = _completed_ids(steps)
    blocked = [step for step in steps if step.get("status") == "failed"]
    if blocked:
        update = {
            "loop_status": "blocked",
            "current_step_id": blocked[0]["id"],
            "replan_trigger": "step_failed",
        }
        next_state = {**state, **update}
        update.update(StateManager(next_state).runtime_update(last_node="step_scheduler"))
        update.update(StateValidator(next_state).validation_update())
        return update

    for idx, step in enumerate(steps):
        if step.get("status") not in {"pending", "running"}:
            continue
        deps = step.get("dependencies", [])
        if not all(dep in completed for dep in deps):
            continue

        step = dict(step)
        step["status"] = "running"
        steps[idx] = step
        update = _sync_plan_and_stack(state, steps, idx, "running")
        next_state = {
            **state,
            **update,
            "current_step_id": step["id"],
        }
        update.update(
            {
                "current_step_id": step["id"],
                "loop_status": "running",
                "policy_violation": None,
                "step_context": build_step_context_packet(next_state),
                **StateManager(next_state).runtime_update(last_node="step_scheduler"),
                **StateValidator(next_state).validation_update(),
            }
        )
        return update

    all_done = all(step.get("status") in {"completed", "skipped"} for step in steps)
    if all_done:
        return {
            **_sync_plan_and_stack(state, steps, None, "completed"),
            "loop_status": "completed",
            "current_step_id": None,
            **StateManager({**state, "loop_status": "completed", "current_step_id": None}).runtime_update(last_node="step_scheduler"),
        }

    blocked_update = {
        "loop_status": "blocked",
        "replan_trigger": "no_runnable_step",
    }
    blocked_state = {**state, **blocked_update}
    blocked_update.update(StateManager(blocked_state).runtime_update(last_node="step_scheduler"))
    blocked_update.update(StateValidator(blocked_state).validation_update())
    return blocked_update


def _current_step_from_state(state: AgentState) -> TaskStep | None:
    steps = _plan_steps(state)
    step_id = state.get("current_step_id")
    if step_id:
        for step in steps:
            if step.get("id") == step_id:
                return step
    idx = state.get("current_task_index", 0)
    if steps and idx < len(steps):
        return steps[idx]
    return None


def _pending_approval_from_blocked_decision(
    blocked: list[dict[str, Any]],
    current_step: TaskStep | None,
    state: AgentState,
) -> dict[str, Any] | None:
    approval_decision = next(
        (decision for decision in blocked if decision.get("decision") == "require_approval"),
        None,
    )
    if not approval_decision:
        return None
    payload = approval_decision.get("approval_payload") or {}
    db_env = state.get("database_environment") or {}
    return {
        "id": f"approval-{uuid.uuid4().hex[:12]}",
        "step_id": str(payload.get("step_id") or (current_step or {}).get("id") or ""),
        "status": "pending",
        "risk_level": approval_decision.get("risk_level", "high"),
        "target_environment": str(payload.get("target_environment") or db_env.get("environment_name") or "unknown"),
        "sql_preview": payload.get("sql_preview"),
        "sql_hash": payload.get("sql_hash"),
        "impact_summary": payload.get("impact_summary"),
        "rollback_summary": payload.get("rollback_summary"),
        "verification_criteria": list(payload.get("verification_criteria") or (current_step or {}).get("success_criteria", [])),
        "user_message": (
            "Review and approve this database action before execution. "
            f"Tool={payload.get('tool_name') or approval_decision.get('tool_name')}"
        ),
        "created_at": _now_iso(),
        "resolved_at": None,
    }


def _can_continue_no_tools_step(current_step: TaskStep | None) -> bool:
    if not current_step:
        return False
    phase = str(current_step.get("phase") or "")
    operation = str(current_step.get("operation_type") or "")
    return phase in {"propose", "report", "approve", "clarify"} or operation == "documentation"


def _clear_message_tool_calls(message: Any) -> None:
    """Clear tool calls in object or serialized message shapes in-place."""
    if isinstance(message, dict):
        message["tool_calls"] = []
        additional = message.get("additional_kwargs")
        if isinstance(additional, dict):
            additional.pop("tool_calls", None)
        return
    if hasattr(message, "tool_calls"):
        message.tool_calls = []


def tool_policy_gate(state: AgentState) -> dict[str, Any]:
    """Enforce the current step's tool policy before execution."""
    messages = state.get("messages", [])
    if not messages:
        return {}

    last_msg = messages[-1]
    tool_calls = tool_call_items(last_msg)
    if not tool_calls:
        return {}

    environment_update = ExecutionEnvironmentManager(state).bootstrap_state()
    policy_state = {**state, **environment_update}
    current_step = _current_step_from_state(policy_state)
    packet = policy_state.get("step_context")
    if current_step and packet and packet.get("step_id") != current_step.get("id"):
        packet = None
    packet = packet or build_step_context_packet(policy_state)
    full_decisions = [evaluate_tool_call_full(policy_state, call) for call in tool_calls]
    decisions = [item[0] for item in full_decisions]
    security_decisions = [item[1] for item in full_decisions]
    sql_reports = [item[2] for item in full_decisions if item[2]]
    approval_bindings = [item[3] for item in full_decisions if item[3]]
    safety_engine = SecurityPolicyEngine(policy_state)
    safety_audit_records = [
        safety_engine.audit_for_decision(
            security_decision,
            step_id=(current_step or {}).get("id"),
            tool_name=decision.get("tool_name"),
        )
        for decision, security_decision in zip(decisions, security_decisions)
    ]
    blocked = [decision for decision in decisions if decision["decision"] in {"deny", "require_approval", "require_clarification"}]
    records = [
        make_invocation_record(
            policy_state,
            call,
            decision,
            status="denied" if decision["decision"] in {"deny", "require_clarification"} else "pending",
        )
        for call, decision in zip(tool_calls, decisions)
    ]
    collaboration = CollaborationManager(policy_state)
    tool_events = collaboration.tool_call_events(decisions)

    if not blocked:
        return {
            "policy_violation": None,
            "tool_policy_decisions": decisions,
            "security_policy_decisions": security_decisions,
            "sql_safety_reports": sql_reports,
            "approval_bindings": approval_bindings,
            "safety_audit_records": safety_audit_records,
            "tool_invocation_records": records,
            "collaboration_events": tool_events,
            "step_context": packet,
            "output_safety_policy": safety_engine.default_output_policy(),
            **environment_update,
            **StateManager({**policy_state, **environment_update}).runtime_update(last_node="tool_policy_gate"),
            **StateValidator({**policy_state, **environment_update}).validation_update(),
        }

    violation = blocked[0]["reason"]
    policy = str((packet or {}).get("tool_policy") or (current_step or {}).get("tool_policy", "no_tools"))
    pending_approval = _pending_approval_from_blocked_decision(blocked, current_step, policy_state)

    logger.warning("Tool policy violation: %s", violation)

    if pending_approval:
        result = {
            "policy_violation": None,
            "tool_policy_decisions": decisions,
            "security_policy_decisions": security_decisions,
            "sql_safety_reports": sql_reports,
            "approval_bindings": approval_bindings,
            "safety_audit_records": safety_audit_records,
            "tool_invocation_records": records,
            "step_context": packet,
            "output_safety_policy": safety_engine.default_output_policy(),
            **environment_update,
            "loop_status": "waiting_for_approval",
            "pending_approval": pending_approval,
            "approval_decisions": [pending_approval],
        }
        approval_update = CollaborationManager({**policy_state, **result}).approval_card_update(pending_approval)
        result["approval_card"] = approval_update["approval_card"]
        result["collaboration_events"] = [*tool_events, *approval_update["collaboration_events"]]
        waiting_state = {
            **policy_state,
            **environment_update,
            **{
                "loop_status": "waiting_for_approval",
                "pending_approval": pending_approval,
                "approval_card": result.get("approval_card"),
                "policy_violation": None,
            },
        }
        result.update(StateManager(waiting_state).runtime_update(last_node="tool_policy_gate"))
        result.update(StateValidator(waiting_state).validation_update())
        return result

    if policy == "no_tools" and _can_continue_no_tools_step(current_step):
        _clear_message_tool_calls(last_msg)
        tool_names = [str(call.get("name", "unknown")) for call in tool_calls]
        content = (
            f"The current {current_step.get('phase') or 'planned'} step does not allow additional tool calls.\n"
            f"Blocked tools: {', '.join(tool_names)}\n"
            "Continue using the evidence already collected in structured observations and tool results. "
            "If evidence is insufficient, state the limitation and next safe step instead of calling another tool."
        )
        report_state = {**policy_state, **environment_update}
        return {
            "messages": [last_msg, AIMessage(content=content)],
            "policy_violation": None,
            "tool_policy_decisions": decisions,
            "security_policy_decisions": security_decisions,
            "sql_safety_reports": sql_reports,
            "approval_bindings": approval_bindings,
            "safety_audit_records": safety_audit_records,
            "tool_invocation_records": records,
            "step_context": packet,
            "output_safety_policy": safety_engine.default_output_policy(),
            **environment_update,
            "loop_status": "running",
            "collaboration_events": [*tool_events],
            **StateManager(report_state).runtime_update(last_node="tool_policy_gate"),
            **StateValidator(report_state).validation_update(),
        }

    _clear_message_tool_calls(last_msg)

    tool_names = [str(call.get("name", "unknown")) for call in tool_calls]
    content = (
        f"Blocked tool call(s) by plan policy: {violation}\n"
        f"Blocked tools: {', '.join(tool_names)}\n"
        "Continue the current step without these tools, or explain what information is missing."
    )
    result = {
        "messages": [last_msg, AIMessage(content=content)],
        "policy_violation": {
            "step_id": (current_step or {}).get("id"),
            "policy": policy,
            "message": violation,
            "blocked_tools": tool_names,
            "blocked_actions": (packet or {}).get("blocked_actions", []),
            "decisions": blocked,
        },
        "tool_policy_decisions": decisions,
        "security_policy_decisions": security_decisions,
        "sql_safety_reports": sql_reports,
        "approval_bindings": approval_bindings,
        "safety_audit_records": safety_audit_records,
        "tool_invocation_records": records,
        "step_context": packet,
        "output_safety_policy": safety_engine.default_output_policy(),
        **environment_update,
        "loop_status": "blocked" if policy == "write_tools_after_approval" else "running",
    }
    safety_update = collaboration.safety_block_update(
        step_id=(current_step or {}).get("id"),
        violation=result.get("policy_violation"),
        decisions=blocked,
    )
    result["collaboration_events"] = [*tool_events, *safety_update["collaboration_events"]]
    blocked_state = {
        **policy_state,
        **environment_update,
        **{
            "loop_status": result["loop_status"],
            "policy_violation": result.get("policy_violation"),
        },
    }
    result.update(StateManager(blocked_state).runtime_update(last_node="tool_policy_gate"))
    result.update(StateValidator(blocked_state).validation_update())
    return result


def _observation_type(name: str, content: str) -> str:
    lowered = f"{name} {content}".lower()
    if "explain" in lowered:
        return "explain_plan"
    if "schema" in lowered:
        return "schema_summary"
    if "index" in lowered:
        return "index_summary"
    if "lock" in lowered:
        return "lock_wait"
    if "affected" in lowered or "rows" in lowered and any(word in lowered for word in ("update", "delete", "insert")):
        return "affected_rows"
    if "error" in lowered or "exception" in lowered:
        return "sql_error" if "sql" in lowered or "postgres" in lowered else "tool_error"
    return "query_result"


def _successful_step_observations(state: AgentState, step_id: str) -> list[DBObservation]:
    return [
        obs
        for obs in state.get("db_observations", [])
        if obs.get("step_id") == step_id and obs.get("payload", {}).get("success") is not False
    ]


def _successful_observations(state: AgentState) -> list[DBObservation]:
    return [
        obs
        for obs in state.get("db_observations", [])
        if obs.get("payload", {}).get("success") is not False
    ]


def _step_observations(state: AgentState, step_id: str) -> list[DBObservation]:
    return [
        obs
        for obs in state.get("db_observations", [])
        if obs.get("step_id") == step_id
    ]


def _evidence_capabilities(observations: list[DBObservation]) -> set[str]:
    capabilities: set[str] = set()
    for obs in observations:
        obs_type = str(obs.get("type") or "")
        source_tool = str(obs.get("source_tool") or "")
        payload = obs.get("payload") or {}
        capabilities.add(obs_type)
        capabilities.add(source_tool)

        if obs_type == "explain_plan" or source_tool == "postgres_explain" or payload.get("plan"):
            capabilities.add("execution_plan")
        if obs_type in {"schema_summary", "object_detail"} or source_tool in {
            "postgres_object_detail",
            "postgres_list_objects",
            "postgres_list_schemas",
        }:
            capabilities.add("schema_summary")
        if obs_type in {"index_summary", "object_detail"} or source_tool in {
            "postgres_object_detail",
            "postgres_index_advisor",
            "postgres_hypothetical_index_test",
        }:
            capabilities.add("index_summary")
        if source_tool == "postgres_top_queries" or obs_type == "top_queries":
            capabilities.add("top_queries")
        if obs_type == "connection_status" or source_tool == "postgres_connection_check":
            capabilities.add("connection_status")
        if obs_type == "query_result" or source_tool == "postgres_query_readonly":
            capabilities.add("query_result")
            rows = payload.get("rows")
            if rows:
                capabilities.add("row_count_or_statistics")
                capabilities.add("statistics")
    return capabilities


def _required_evidence_satisfied(required: str, capabilities: set[str]) -> bool:
    key = str(required or "")
    if key in capabilities:
        return True
    lowered = key.lower()
    if any(token in lowered for token in ("pg_stat", "top sql", "top query", "slow query", "高成本", "慢 sql", "慢查询")):
        return bool({"top_queries", "postgres_top_queries"} & capabilities)
    if "explain" in lowered or "执行计划" in key:
        return bool({"execution_plan", "explain_plan", "postgres_explain"} & capabilities)
    if any(token in lowered for token in ("schema", "ddl", "metadata")) or any(token in key for token in ("表结构", "元数据", "列定义")):
        return bool({"schema_summary", "object_detail", "postgres_object_detail", "postgres_list_objects"} & capabilities)
    if "index" in lowered or "索引" in key:
        return bool({"index_summary", "object_detail", "postgres_object_detail", "postgres_index_advisor"} & capabilities)
    if any(token in lowered for token in ("row count", "statistics", "statistic")) or any(token in key for token in ("行数", "统计")):
        return bool({"row_count_or_statistics", "statistics", "query_result", "postgres_query_readonly"} & capabilities)
    if "database" in lowered or "数据库" in key:
        return bool({"database_list", "postgres_list_databases", "connection_status"} & capabilities)
    aliases = {
        "execution_plan": {"execution_plan", "explain_plan", "postgres_explain"},
        "schema_summary": {"schema_summary", "object_detail", "postgres_object_detail", "postgres_list_objects"},
        "index_summary": {"index_summary", "object_detail", "postgres_object_detail", "postgres_index_advisor"},
        "row_count_or_statistics": {"row_count_or_statistics", "statistics", "query_result", "postgres_query_readonly"},
        "active_queries": {"top_queries", "postgres_top_queries", "query_result", "postgres_query_readonly"},
        "top_queries": {"top_queries", "postgres_top_queries"},
        "database_list": {"database_list", "postgres_list_databases"},
    }
    return bool(aliases.get(key, set()) & capabilities)


def _tool_step_map(state: AgentState) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for record in state.get("tool_invocation_records", []):
        call_id = str(record.get("call_id") or "")
        step_id = str(record.get("step_id") or "")
        if call_id and step_id:
            mapping[call_id] = step_id
    return mapping


def _failed_step_results(state: AgentState, step_id: str) -> list[dict[str, Any]]:
    mapping = _tool_step_map(state)
    results: list[dict[str, Any]] = []
    for result in state.get("tool_execution_results", []):
        if result.get("success") is not False:
            continue
        call_id = str(result.get("tool_call_id") or "")
        if mapping and mapping.get(call_id) != step_id:
            continue
        results.append(result)
    return results


def _recoverable_read_failure(step: TaskStep, failed_results: list[dict[str, Any]]) -> bool:
    if not failed_results:
        return False
    if step.get("phase") in {"execute", "approve"} or step.get("tool_policy") == "write_tools_after_approval":
        return False
    if step.get("operation_type") not in {"diagnostic", "read_only", "documentation", None}:
        return False
    recoverable_types = {"sql_error", "tool_error"}
    recoverable_sqlstates = {"42601", "42703", "42P01", "42P07"}
    for result in failed_results:
        if str(result.get("result_type") or "") not in recoverable_types:
            return False
        sqlstate = str(result.get("sqlstate") or "")
        if sqlstate and sqlstate not in recoverable_sqlstates:
            return False
    return True


def _failure_repeat_count(state: AgentState, step_id: str, failed_results: list[dict[str, Any]]) -> int:
    if not failed_results:
        return 0
    latest = failed_results[-1]
    latest_key = (
        str(latest.get("tool_name") or ""),
        str(latest.get("sqlstate") or ""),
        str(latest.get("summary") or "")[:160],
    )
    count = 0
    for obs in _step_observations(state, step_id):
        payload = obs.get("payload") or {}
        if payload.get("success") is not False:
            continue
        key = (
            str(obs.get("source_tool") or ""),
            str(payload.get("sqlstate") or ""),
            str(obs.get("summary") or "")[:160],
        )
        if key == latest_key:
            count += 1
    return count


def _read_failure_repair_message(step: TaskStep, failed_results: list[dict[str, Any]]) -> SystemMessage:
    latest = failed_results[-1]
    content = (
        "A read-only PostgreSQL diagnostic tool failed while the current step is still recoverable.\n"
        f"- step_id: {step.get('id')}\n"
        f"- tool_name: {latest.get('tool_name')}\n"
        f"- sqlstate: {latest.get('sqlstate') or 'unknown'}\n"
        f"- error: {latest.get('summary')}\n\n"
        "Continue the same user task by correcting the read-only SQL or choosing a safer structured PostgreSQL tool. "
        "Do not repeat the same failing SQL. If a PostgreSQL catalog/view column is uncertain, inspect the catalog or use "
        "structured object/detail tools before querying it."
    )
    return SystemMessage(content=content)


def _latest_assistant_text(state: AgentState) -> str:
    for message in reversed(state.get("messages", []) or []):
        role = message.get("role") if isinstance(message, dict) else getattr(message, "type", "")
        if role not in {"ai", "assistant"} and not isinstance(message, AIMessage):
            continue
        if isinstance(message, ToolMessage):
            continue
        tool_calls = message.get("tool_calls") if isinstance(message, dict) else getattr(message, "tool_calls", None)
        if tool_calls:
            continue
        content = message.get("content", "") if isinstance(message, dict) else getattr(message, "content", "")
        text = str(content or "").strip()
        if text:
            return text
    return ""


def _step_has_expected_evidence(state: AgentState, step: TaskStep, observations: list[DBObservation]) -> tuple[bool, str]:
    expected_tools = {str(tool) for tool in step.get("expected_tools", []) if tool}
    evidence_required = {str(item) for item in step.get("evidence_required", []) if item}

    if step.get("phase") == "report" and not _latest_assistant_text(state):
        return False, "Waiting for assistant report content before completing the report step."

    if step.get("tool_policy") == "read_only_tools" and (expected_tools or evidence_required) and not observations:
        return False, "Waiting for read-only tool evidence before completing the step."

    source_tools = {str(obs.get("source_tool") or "") for obs in observations}
    observation_types = {str(obs.get("type") or "") for obs in observations}
    capabilities = _evidence_capabilities(observations)

    if "postgres_list_objects" in expected_tools and "postgres_list_objects" not in source_tools:
        return False, "Table/object listing has not been collected yet."
    if "postgres_top_queries" in expected_tools and not (
        "postgres_top_queries" in source_tools or "top_queries" in observation_types
    ):
        return False, "Top/slow query evidence has not been collected yet."

    if "schema_summary" in evidence_required and (
        expected_tools
        & {"postgres_list_objects", "postgres_list_schemas", "postgres_object_detail", "postgres_read"}
    ) and not _required_evidence_satisfied("schema_summary", capabilities):
        return False, "Schema evidence has not been collected yet."
    if "top_queries" in evidence_required and not (
        "top_queries" in observation_types or "postgres_top_queries" in source_tools
    ):
        return False, "Top/slow query evidence has not been collected yet."
    if "database_list" in evidence_required and not observations:
        return False, "Database list evidence has not been collected yet."
    missing_required = [
        item for item in evidence_required
        if not _required_evidence_satisfied(item, capabilities)
    ]
    if missing_required:
        return False, f"Waiting for required evidence: {', '.join(missing_required)}."

    return True, "Step completed against available success criteria."


def normalize_observation(state: AgentState) -> dict[str, Any]:
    """Convert ToolMessages into structured observations."""
    step = _current_step_from_state(state)
    step_id = (step or {}).get("id") or state.get("current_step_id") or ""
    observations: list[DBObservation] = []
    digests: list[ResultDigest] = []
    seen_tool_call_ids = {
        obs.get("payload", {}).get("tool_call_id")
        for obs in state.get("db_observations", [])
        if obs.get("payload", {}).get("tool_call_id")
    }

    for result in state.get("tool_execution_results", []):
        tool_call_id = result.get("tool_call_id")
        if tool_call_id and tool_call_id in seen_tool_call_ids:
            continue
        observation: DBObservation = {
            "id": f"obs-{uuid.uuid4().hex[:12]}",
            "step_id": step_id,
            "type": result.get("result_type", "tool_error"),
            "source_tool": result.get("tool_name", "tool"),
            "summary": result.get("summary", "")[:300],
            "payload": {
                **result.get("payload", {}),
                "tool_call_id": tool_call_id,
                "duration_ms": result.get("duration_ms"),
                "success": result.get("success"),
                "sqlstate": result.get("sqlstate"),
                "result_type": result.get("result_type"),
                "row_count": result.get("row_count"),
                "affected_rows": result.get("affected_rows"),
            },
            "created_at": _now_iso(),
        }
        observations.append(observation)
        if tool_call_id:
            seen_tool_call_ids.add(tool_call_id)
        digest = build_result_digest(observation)
        if digest:
            digests.append(digest)

    for msg in state.get("messages", []):
        if not isinstance(msg, ToolMessage):
            continue
        tool_call_id = getattr(msg, "tool_call_id", None)
        if tool_call_id and tool_call_id in seen_tool_call_ids:
            continue
        content = str(getattr(msg, "content", ""))
        name = str(getattr(msg, "name", "tool"))
        observation: DBObservation = {
            "id": f"obs-{uuid.uuid4().hex[:12]}",
            "step_id": step_id,
            "type": _observation_type(name, content),
            "source_tool": name,
            "summary": content[:300],
            "payload": {
                "content": content,
                "tool_call_id": tool_call_id,
            },
            "created_at": _now_iso(),
        }
        observations.append(observation)
        digest = build_result_digest(observation)
        if digest:
            digests.append(digest)

    if not observations:
        return {}

    snapshot_state = {
        **state,
        "db_observations": [*state.get("db_observations", []), *observations],
        "result_digests": [*state.get("result_digests", []), *digests],
    }
    manager_update = StateManager(state).record_observations(observations, digests)
    snapshot_state = {**snapshot_state, **manager_update}
    return {
        **manager_update,
        "collaboration_events": CollaborationManager(snapshot_state).observation_events(observations),
        "context_snapshots": [build_context_snapshot(snapshot_state)],
        **StateValidator(snapshot_state).validation_update(),
    }


def verify_step(state: AgentState) -> dict[str, Any]:
    """Check current step completion against available evidence."""
    steps = _plan_steps(state)
    if not steps:
        return {}

    current_idx = _find_current_index(steps, state.get("current_step_id"))
    if current_idx >= len(steps):
        return {}

    step = dict(steps[current_idx])
    if step.get("status") in {"completed", "skipped", "failed"}:
        return {}

    step_observations = _successful_step_observations(state, step.get("id"))
    phase = str(step.get("phase") or "")
    can_use_prior_evidence = phase in {"diagnose", "propose", "report"} and bool(_latest_assistant_text(state))
    observations = step_observations or (_successful_observations(state) if can_use_prior_evidence else [])
    failed_results = _failed_step_results(state, step.get("id"))
    policy_violation = state.get("policy_violation")
    criteria = step.get("success_criteria", [])

    if policy_violation and policy_violation.get("step_id") == step.get("id"):
        status = "blocked"
        summary = policy_violation.get("message", "Step blocked by tool policy.")
        step["status"] = "failed"
        step["error"] = summary
    elif failed_results and not observations and _recoverable_read_failure(step, failed_results):
        repeat_count = _failure_repeat_count(state, step["id"], failed_results)
        if repeat_count < 1:
            repair_message = _read_failure_repair_message(step, failed_results)
            repair_state = {**state, "messages": [*state.get("messages", []), repair_message]}
            return {
                "messages": [repair_message],
                "loop_status": "running",
                "step_context": build_step_context_packet(repair_state),
                **StateManager(repair_state).runtime_update(last_node="verify_step"),
                **StateValidator(repair_state).validation_update(),
            }
        status = "blocked"
        summary = str(failed_results[-1].get("summary") or "Read-only diagnostic failed repeatedly.")
        step["status"] = "failed"
        step["error"] = summary
    elif failed_results and not observations:
        status = "blocked"
        summary = str(failed_results[-1].get("summary") or "Tool execution failed.")
        step["status"] = "failed"
        step["error"] = summary
    else:
        evidence_ok, evidence_summary = _step_has_expected_evidence(state, step, observations)
        if not evidence_ok:
            return {
                "loop_status": "running",
                "step_context": build_step_context_packet(state),
                **StateManager(state).runtime_update(last_node="verify_step"),
                **StateValidator(state).validation_update(),
            }
        status = "passed"
        summary = evidence_summary
        step["status"] = "completed"
        step["result"] = observations[-1]["summary"] if observations else _latest_assistant_text(state) or "Completed without tool evidence."

    steps[current_idx] = step
    result: VerificationResult = {
        "id": f"verify-{uuid.uuid4().hex[:12]}",
        "step_id": step["id"],
        "status": status,
        "criteria_checked": list(criteria),
        "evidence_ids": [obs["id"] for obs in observations],
        "summary": summary,
        "created_at": _now_iso(),
    }
    is_final_report = step.get("phase") == "report" and status == "passed"

    environment_update = ExecutionEnvironmentManager(state).bootstrap_state()
    verification_artifact = ArtifactStore(environment_update.get("task_workspace")).record(
        kind="verification_evidence",
        summary=summary,
        payload_ref=result["id"],
        sensitivity="internal",
        lifecycle="persistent",
    )
    if environment_update.get("task_workspace"):
        task_workspace = dict(environment_update["task_workspace"])
        task_workspace["artifact_ids"] = [
            *list(task_workspace.get("artifact_ids", [])),
            verification_artifact["id"],
        ]
        task_workspace["updated_at"] = _now_iso()
        environment_update["task_workspace"] = task_workspace

    update = _sync_plan_and_stack(
        state,
        steps,
        current_idx,
        "failed" if status in {"failed", "blocked"} else "running",
    )
    snapshot_state = {
        **state,
        **update,
        **environment_update,
        "verification_results": [*state.get("verification_results", []), result],
        "artifact_records": [*state.get("artifact_records", []), verification_artifact],
    }
    memory_updates = consolidate_memories(snapshot_state) if status == "passed" else {}
    error_recovery_updates: dict[str, Any] = {}
    if status in {"failed", "blocked"}:
        classifier = ErrorClassifier(snapshot_state)
        error_record = classifier.from_policy_violation()
        if not error_record and failed_results:
            error_record = classifier.from_tool_result(failed_results[-1])
        if error_record:
            error_recovery_updates = RecoveryEngine(snapshot_state).update_for_error(error_record)
    update.update(
        {
            "loop_status": "blocked" if status in {"failed", "blocked"} else "running",
            "replan_trigger": "verification_blocked" if status in {"failed", "blocked"} else None,
            "step_context": build_step_context_packet(snapshot_state),
            "context_snapshots": [build_context_snapshot(snapshot_state)],
            "collaboration_events": [
                CollaborationManager(snapshot_state).verification_event(
                    result,
                    is_final_report=is_final_report,
                )
            ],
            **StateManager(state).record_verification(result, verification_artifact, environment_update),
            **memory_updates,
            **error_recovery_updates,
        }
    )
    final_state = {**state, **update}
    update.update(StateValidator(final_state).validation_update())
    return update
