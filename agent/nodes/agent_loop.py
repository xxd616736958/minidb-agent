"""Plan-step driven Agent Loop nodes for PostgreSQL management workflows."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from langchain_core.messages import AIMessage, ToolMessage

from agent.context import (
    build_context_snapshot,
    build_result_digest,
    build_step_context_packet,
    retrieve_relevant_memories,
)
from agent.state import AgentState, DBObservation, ResultDigest, TaskStep, VerificationResult
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

    if not blocked:
        return {
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
            **StateManager({**policy_state, **environment_update}).runtime_update(last_node="tool_policy_gate"),
            **StateValidator({**policy_state, **environment_update}).validation_update(),
        }

    violation = blocked[0]["reason"]
    policy = str((packet or {}).get("tool_policy") or (current_step or {}).get("tool_policy", "no_tools"))
    pending_approval = _pending_approval_from_blocked_decision(blocked, current_step, policy_state)

    logger.warning("Tool policy violation: %s", violation)

    if hasattr(last_msg, "tool_calls"):
        last_msg.tool_calls = []

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
        "loop_status": "waiting_for_approval" if pending_approval else ("blocked" if policy == "write_tools_after_approval" else "running"),
    }
    if pending_approval:
        result["pending_approval"] = pending_approval
        result["approval_decisions"] = [pending_approval]
    blocked_state = {
        **policy_state,
        **environment_update,
        **{
            "loop_status": result["loop_status"],
            "pending_approval": pending_approval or policy_state.get("pending_approval"),
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

    observations = [
        obs for obs in state.get("db_observations", [])
        if obs.get("step_id") == step.get("id")
    ]
    policy_violation = state.get("policy_violation")
    criteria = step.get("success_criteria", [])

    if policy_violation and policy_violation.get("step_id") == step.get("id"):
        status = "blocked"
        summary = policy_violation.get("message", "Step blocked by tool policy.")
        step["status"] = "failed"
        step["error"] = summary
    else:
        status = "passed"
        summary = "Step completed against available success criteria."
        step["status"] = "completed"
        step["result"] = observations[-1]["summary"] if observations else "Completed without tool evidence."

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
    update.update(
        {
            "loop_status": "blocked" if status in {"failed", "blocked"} else "running",
            "replan_trigger": "verification_blocked" if status in {"failed", "blocked"} else None,
            "step_context": build_step_context_packet(snapshot_state),
            "context_snapshots": [build_context_snapshot(snapshot_state)],
            **StateManager(state).record_verification(result, verification_artifact, environment_update),
            **memory_updates,
        }
    )
    final_state = {**state, **update}
    update.update(StateValidator(final_state).validation_update())
    return update
