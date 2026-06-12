"""Tool executor node — runs approved tool calls using LangGraph's ToolNode.

This node uses the official langgraph.prebuilt.ToolNode which handles:
  - Dispatching tool calls to the correct tool instance
  - Parallel execution of multiple tool calls
  - Creating ToolMessage results that update the message history
  - Error handling per tool call
"""

from __future__ import annotations

import logging
import re
from typing import Any
from datetime import datetime, timezone

from langchain_core.messages import ToolMessage

from agent.state import AgentState
from execution.environment import ArtifactStore, ExecutionEnvironmentManager
from safety.engine import SecurityPolicyEngine
from state_management.manager import StateManager
from state_management.validator import StateValidator
from tools.postgres.results import loads_result
from tools.registry import registry

logger = logging.getLogger(__name__)

# Lazy-initialized ToolNode
_tool_node = None


def _get_tool_node():
    """Get or create the ToolNode with current registered tools."""
    global _tool_node
    from langgraph.prebuilt import ToolNode

    tools = registry.get_all()
    # Recreate if tools changed (plugin hot-reload scenario)
    if _tool_node is None or len(_tool_node.tools_by_name) != len(tools):
        _tool_node = ToolNode(tools)
        logger.debug(f"ToolNode initialized with {len(tools)} tools")
    return _tool_node


def _result_type(tool_name: str, content: str) -> str:
    lowered = f"{tool_name} {content}".lower()
    if "policy_denied" in lowered or "blocked" in lowered:
        return "policy_denied"
    if "explain" in lowered:
        return "explain_plan"
    if "schema" in lowered:
        return "schema_summary"
    if "index" in lowered:
        return "index_summary"
    if "lock" in lowered:
        return "lock_wait"
    if "affected" in lowered or ("rows" in lowered and any(word in lowered for word in ("update", "delete", "insert"))):
        return "affected_rows"
    if "sqlstate" in lowered or "sql" in lowered and "error" in lowered:
        return "sql_error"
    if "error" in lowered or "exception" in lowered:
        return "tool_error"
    return "query_result"


def _tool_execution_result(
    msg: ToolMessage,
    started_at: datetime,
    environment_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    content = str(getattr(msg, "content", ""))
    name = str(getattr(msg, "name", "tool"))
    duration_ms = int((datetime.now(timezone.utc) - started_at).total_seconds() * 1000)
    structured = loads_result(content)
    if structured:
        payload = dict(structured.get("payload", {}))
        if environment_summary:
            payload["execution_environment"] = environment_summary
        return {
            "tool_call_id": str(getattr(msg, "tool_call_id", "")),
            "tool_name": structured.get("tool_name") or name,
            "success": structured.get("success", False),
            "result_type": structured.get("result_type", "tool_error"),
            "summary": structured.get("summary", "")[:300],
            "payload": payload,
            "row_count": structured.get("row_count"),
            "affected_rows": structured.get("affected_rows"),
            "sqlstate": structured.get("sqlstate"),
            "duration_ms": structured.get("duration_ms") or duration_ms,
            "truncated": structured.get("truncated", False),
            "sensitive_fields_masked": structured.get("sensitive_fields_masked", []),
        }
    result_type = _result_type(name, content)
    success = result_type not in {"sql_error", "tool_error", "policy_denied"} and not content.lower().startswith("error")
    return {
        "tool_call_id": str(getattr(msg, "tool_call_id", "")),
        "tool_name": name,
        "success": success,
        "result_type": result_type,
        "summary": content[:300],
        "payload": {"content": content, **({"execution_environment": environment_summary} if environment_summary else {})},
        "row_count": None,
        "affected_rows": None,
        "sqlstate": None,
        "duration_ms": duration_ms,
        "truncated": len(content) > 300,
        "sensitive_fields_masked": [],
    }


def _artifact_kind_for_result(result: dict[str, Any]) -> str:
    result_type = str(result.get("result_type") or "")
    tool_name = str(result.get("tool_name") or "")
    summary = str(result.get("summary") or "")
    if tool_name == "file_write":
        lowered = summary.lower()
        if ".sql" in lowered:
            return "sql_draft"
        if ".md" in lowered or "report" in lowered:
            return "final_report"
        return "execution_log"
    if result_type == "explain_plan":
        return "explain_json"
    if result_type == "health_report":
        return "health_report"
    if result_type == "dry_run_report":
        return "approval_snapshot"
    if result_type in {"query_result", "schema_summary", "object_detail", "top_queries", "lock_report", "index_advice"}:
        return "query_result_digest"
    if result_type in {"write_result", "maintenance_result", "policy_denied", "sql_error", "tool_error"}:
        return "execution_log"
    return "execution_log"


def _artifact_path_for_result(result: dict[str, Any]) -> str | None:
    if str(result.get("tool_name") or "") != "file_write":
        return None
    match = re.search(r"file:\s+(.+?)\s+\(", str(result.get("summary") or ""))
    return match.group(1) if match else None


def _update_invocation_records(
    state: AgentState,
    results: list[dict[str, Any]],
    artifacts: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    if not results:
        return []
    by_call_id = {result["tool_call_id"]: result for result in results}
    artifact_ids_by_call_id: dict[str, list[str]] = {}
    for artifact in artifacts or []:
        call_id = artifact.get("payload_ref")
        if call_id:
            artifact_ids_by_call_id.setdefault(str(call_id), []).append(str(artifact.get("id")))
    updated = []
    now = datetime.now(timezone.utc).isoformat()
    for record in state.get("tool_invocation_records", []):
        call_id = record.get("call_id")
        if call_id not in by_call_id:
            continue
        result = by_call_id[call_id]
        record = dict(record)
        record["ended_at"] = now
        record["status"] = "succeeded" if result.get("success") else "failed"
        record["duration_ms"] = result.get("duration_ms")
        record["result_ref"] = call_id
        record["artifact_ids"] = artifact_ids_by_call_id.get(str(call_id), [])
        environment_summary = result.get("payload", {}).get("execution_environment")
        if environment_summary:
            record["environment_summary"] = environment_summary
        if not result.get("success"):
            record["error_type"] = result.get("result_type")
            record["error_message"] = result.get("summary")
        updated.append(record)
    return updated


def execute_tools(state: AgentState) -> dict[str, Any]:
    """Execute approved tool calls from the last LLM message.

    This node:
      1. Extracts tool_calls from the last AIMessage
      2. Dispatches each to the correct tool via ToolNode
      3. Collects results as ToolMessages
      4. Updates task status if we're in a plan

    Returns:
        Partial state with ToolMessages appended and results logged.
    """
    messages = state.get("messages", [])
    if not messages:
        logger.warning("execute_tools called with no messages")
        return {"error": "No messages in state", "step_count": state.get("step_count", 0) + 1}

    last_msg = messages[-1]
    tool_calls = getattr(last_msg, "tool_calls", None)
    if not tool_calls:
        logger.warning("execute_tools called but last message has no tool_calls")
        return {"step_count": state.get("step_count", 0) + 1}

    logger.info(
        f"Executing {len(tool_calls)} tool call(s): "
        f"{[tc['name'] for tc in tool_calls]}"
    )

    env_manager = ExecutionEnvironmentManager(state)
    environment_update = env_manager.bootstrap_state()
    environment_summary = env_manager.invocation_environment_summary()

    # Execute via LangGraph's ToolNode
    started_at = datetime.now(timezone.utc)
    try:
        tool_node = _get_tool_node()
        result = tool_node.invoke({"messages": messages})
    except Exception as e:
        logger.error(f"ToolNode execution failed: {e}")
        return {
            "error": f"Tool execution failed: {e}",
            "step_count": state.get("step_count", 0) + 1,
        }

    # Extract results and format for logging
    new_messages = result.get("messages", [])
    tool_results: list[str] = []
    structured_results: list[dict[str, Any]] = []
    for msg in new_messages:
        if hasattr(msg, "name") and hasattr(msg, "content"):
            content_preview = str(msg.content)[:200]
            tool_results.append(f"[{msg.name}]: {content_preview}")
        if isinstance(msg, ToolMessage):
            structured_results.append(_tool_execution_result(msg, started_at, environment_summary))

    safety_engine = SecurityPolicyEngine({**state, **environment_update})
    output_safety_decisions = [
        safety_engine.output_handling_decision(
            tool_name=str(result.get("tool_name") or "tool"),
            result=result,
        )
        for result in structured_results
    ]
    output_safety_audits = [
        safety_engine.audit_for_decision(
            decision,
            step_id=state.get("current_step_id"),
            tool_name=decision.get("subject"),
        )
        for decision in output_safety_decisions
        if decision.get("matched_rules") and decision.get("matched_rules") != ["output.safe"]
    ]

    artifact_records = [
        ArtifactStore(environment_update.get("task_workspace")).record(
            kind=_artifact_kind_for_result(result),
            path=_artifact_path_for_result(result),
            summary=f"{result.get('tool_name')} -> {result.get('result_type')} ({'ok' if result.get('success') else 'failed'})",
            payload_ref=result.get("tool_call_id"),
            sensitivity="internal",
            lifecycle="persistent",
        )
        for result in structured_results
    ]
    workspace_update = StateManager(state).record_artifacts_on_task_workspace(
        artifact_records,
        environment_update.get("task_workspace"),
    )
    environment_update.update(workspace_update)

    # Keep task running; verify_step decides completion after observations are
    # normalized and checked against success criteria.
    task_stack = list(state.get("task_stack", []))
    current_idx = state.get("current_task_index", 0)
    if task_stack and current_idx < len(task_stack):
        task = dict(task_stack[current_idx])
        if not state.get("error"):
            task["result"] = tool_results[-1] if tool_results else ""
        task_stack[current_idx] = task

    db_task_plan = state.get("db_task_plan")
    if db_task_plan and current_idx < len(db_task_plan.get("steps", [])):
        db_task_plan = dict(db_task_plan)
        plan_steps = [dict(step) for step in db_task_plan.get("steps", [])]
        if current_idx < len(plan_steps):
            plan_steps[current_idx]["status"] = task_stack[current_idx].get("status", "completed")
            plan_steps[current_idx]["result"] = task_stack[current_idx].get("result")
        db_task_plan["steps"] = plan_steps
        db_task_plan["updated_at"] = datetime.now(timezone.utc).isoformat()

    replay_policies = [
        StateManager(state).replay_policy_for_tool(str(result.get("tool_name") or ""), str(result.get("tool_call_id") or ""))
        for result in structured_results
        if result.get("tool_call_id")
    ]
    final_update = {
        "messages": new_messages,
        "tool_call_results": tool_results,
        "tool_execution_results": structured_results,
        "security_policy_decisions": output_safety_decisions,
        "safety_audit_records": output_safety_audits,
        "output_safety_policy": safety_engine.default_output_policy(),
        "tool_invocation_records": _update_invocation_records(state, structured_results, artifact_records),
        "artifact_records": artifact_records,
        "replay_policies": replay_policies,
        **environment_update,
        "task_stack": task_stack,
        "db_task_plan": db_task_plan,
        "step_count": state.get("step_count", 0) + 1,
    }
    next_state = {**state, **final_update}
    final_update.update(StateManager(next_state).runtime_update(last_node="execute_tools"))
    final_update.update(StateValidator(next_state).validation_update())
    return final_update
