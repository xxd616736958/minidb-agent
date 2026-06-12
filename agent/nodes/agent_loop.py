"""Plan-step driven Agent Loop nodes for PostgreSQL management workflows."""

from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from langchain_core.messages import AIMessage, ToolMessage

from agent.state import AgentState, DBObservation, TaskStep, VerificationResult

logger = logging.getLogger(__name__)


WRITE_SQL_RE = re.compile(
    r"\b(insert|update|delete|merge|alter|drop|truncate|create|grant|revoke|vacuum|reindex)\b",
    re.IGNORECASE,
)
READ_SQL_RE = re.compile(r"\b(select|explain|show|with)\b", re.IGNORECASE)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _plan_steps(state: AgentState) -> list[TaskStep]:
    plan = state.get("db_task_plan")
    if plan and plan.get("steps"):
        return list(plan["steps"])
    return list(state.get("task_stack", []))


def _completed_ids(steps: list[TaskStep]) -> set[str]:
    return {
        step["id"]
        for step in steps
        if step.get("status") in {"completed", "skipped"}
    }


def _find_current_index(steps: list[TaskStep], current_step_id: str | None) -> int:
    if current_step_id:
        for idx, step in enumerate(steps):
            if step.get("id") == current_step_id:
                return idx
    return 0


def _sync_plan_and_stack(
    state: AgentState,
    steps: list[TaskStep],
    current_idx: int | None = None,
    plan_status: str | None = None,
) -> dict[str, Any]:
    update: dict[str, Any] = {
        "task_stack": steps,
    }
    if current_idx is not None:
        update["current_task_index"] = current_idx

    plan = state.get("db_task_plan")
    if plan:
        plan = dict(plan)
        plan["steps"] = steps
        plan["updated_at"] = _now_iso()
        if plan_status:
            plan["status"] = plan_status
        update["db_task_plan"] = plan
    return update


def step_scheduler(state: AgentState) -> dict[str, Any]:
    """Select the next runnable plan step and mark it running."""
    if state.get("error"):
        return {}

    steps = _plan_steps(state)
    if not steps:
        return {"loop_status": "completed", "current_step_id": None}

    completed = _completed_ids(steps)
    blocked = [step for step in steps if step.get("status") == "failed"]
    if blocked:
        return {
            "loop_status": "blocked",
            "current_step_id": blocked[0]["id"],
            "replan_trigger": "step_failed",
        }

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
        update.update(
            {
                "current_step_id": step["id"],
                "loop_status": "running",
                "policy_violation": None,
            }
        )
        return update

    all_done = all(step.get("status") in {"completed", "skipped"} for step in steps)
    if all_done:
        return {
            **_sync_plan_and_stack(state, steps, None, "completed"),
            "loop_status": "completed",
            "current_step_id": None,
        }

    return {
        "loop_status": "blocked",
        "replan_trigger": "no_runnable_step",
    }


def _tool_call_items(msg: Any) -> list[dict[str, Any]]:
    return list(getattr(msg, "tool_calls", None) or [])


def _tool_args_text(tool_call: dict[str, Any]) -> str:
    args = tool_call.get("args") or {}
    if isinstance(args, dict):
        return " ".join(str(value) for value in args.values())
    return str(args)


def _is_postgres_tool(tool_name: str) -> bool:
    lowered = tool_name.lower()
    return any(token in lowered for token in ("postgres", "postgresql", "sql", "database", "db"))


def _is_write_call(tool_call: dict[str, Any]) -> bool:
    name = str(tool_call.get("name", ""))
    text = _tool_args_text(tool_call)
    return _is_postgres_tool(name) and bool(WRITE_SQL_RE.search(text))


def _is_read_call(tool_call: dict[str, Any]) -> bool:
    name = str(tool_call.get("name", ""))
    text = _tool_args_text(tool_call)
    if not _is_postgres_tool(name):
        return True
    return bool(READ_SQL_RE.search(text)) and not bool(WRITE_SQL_RE.search(text))


def _policy_violation_message(policy: str, tool_calls: list[dict[str, Any]]) -> str | None:
    if not tool_calls:
        return None

    if policy == "no_tools":
        return "Current plan step does not allow tool calls."

    if policy == "read_only_tools":
        write_calls = [call for call in tool_calls if _is_write_call(call)]
        if write_calls:
            names = ", ".join(str(call.get("name", "unknown")) for call in write_calls)
            return f"Current plan step is read-only; blocked write-capable SQL call(s): {names}."
        non_read = [call for call in tool_calls if not _is_read_call(call)]
        if non_read:
            names = ", ".join(str(call.get("name", "unknown")) for call in non_read)
            return f"Current plan step allows only read-only database tools; blocked: {names}."

    return None


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


def tool_policy_gate(state: AgentState) -> dict[str, Any]:
    """Enforce the current step's tool policy before execution."""
    messages = state.get("messages", [])
    if not messages:
        return {}

    last_msg = messages[-1]
    tool_calls = _tool_call_items(last_msg)
    if not tool_calls:
        return {}

    current_step = _current_step_from_state(state)
    policy = (current_step or {}).get("tool_policy", "no_tools")
    violation = _policy_violation_message(policy, tool_calls)

    if policy == "write_tools_after_approval":
        approved = any(
            decision.get("step_id") == (current_step or {}).get("id")
            and decision.get("status") == "approved"
            for decision in state.get("approval_decisions", [])
        )
        if not approved and any(_is_write_call(call) for call in tool_calls):
            violation = "Current write-capable PostgreSQL step requires explicit approval before execution."

    if not violation:
        return {"policy_violation": None}

    logger.warning("Tool policy violation: %s", violation)

    if hasattr(last_msg, "tool_calls"):
        last_msg.tool_calls = []

    tool_names = [str(call.get("name", "unknown")) for call in tool_calls]
    content = (
        f"Blocked tool call(s) by plan policy: {violation}\n"
        f"Blocked tools: {', '.join(tool_names)}\n"
        "Continue the current step without these tools, or explain what information is missing."
    )
    return {
        "messages": [last_msg, AIMessage(content=content)],
        "policy_violation": {
            "step_id": (current_step or {}).get("id"),
            "policy": policy,
            "message": violation,
            "blocked_tools": tool_names,
        },
        "loop_status": "blocked" if policy == "write_tools_after_approval" else "running",
    }


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
    seen_tool_call_ids = {
        obs.get("payload", {}).get("tool_call_id")
        for obs in state.get("db_observations", [])
        if obs.get("payload", {}).get("tool_call_id")
    }

    for msg in state.get("messages", []):
        if not isinstance(msg, ToolMessage):
            continue
        tool_call_id = getattr(msg, "tool_call_id", None)
        if tool_call_id and tool_call_id in seen_tool_call_ids:
            continue
        content = str(getattr(msg, "content", ""))
        name = str(getattr(msg, "name", "tool"))
        observations.append(
            {
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
        )

    if not observations:
        return {}
    return {"db_observations": observations}


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

    update = _sync_plan_and_stack(
        state,
        steps,
        current_idx,
        "failed" if status in {"failed", "blocked"} else "running",
    )
    update.update(
        {
            "verification_results": [result],
            "loop_status": "blocked" if status in {"failed", "blocked"} else "running",
            "replan_trigger": "verification_blocked" if status in {"failed", "blocked"} else None,
        }
    )
    return update
