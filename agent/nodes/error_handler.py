"""Structured error handler node for PostgreSQL task self-repair."""

from __future__ import annotations

import logging
from typing import Any

from langchain_core.messages import AIMessage, SystemMessage

from agent.state import AgentState, ErrorRecord, RecoveryDecision
from collaboration.manager import CollaborationManager
from error_handling.classifier import ErrorClassifier
from error_handling.recovery import RecoveryEngine
from state_management.manager import StateManager
from state_management.validator import StateValidator

logger = logging.getLogger(__name__)


def _latest_integrity_error(state: AgentState) -> ErrorRecord | None:
    report = (state.get("state_integrity_reports") or [None])[-1]
    return ErrorClassifier(state).from_integrity_report(report)


def _active_error(state: AgentState) -> ErrorRecord | None:
    classifier = ErrorClassifier(state)
    return (
        classifier.from_state_error(node_name=(state.get("state_metadata") or {}).get("last_node"))
        or classifier.from_policy_violation()
        or _latest_integrity_error(state)
        or ((state.get("error_records") or [None])[-1])
    )


def _system_recovery_message(error: ErrorRecord, decision: RecoveryDecision) -> SystemMessage:
    content = (
        "Structured recovery decision for the current PostgreSQL task:\n"
        f"- error_id: {error.get('id')}\n"
        f"- error_type: {error.get('error_type')}\n"
        f"- source: {error.get('source')}\n"
        f"- step_id: {error.get('step_id')}\n"
        f"- tool_name: {error.get('tool_name')}\n"
        f"- sql_hash: {error.get('sql_hash')}\n"
        f"- recovery_action: {decision.get('action')}\n"
        f"- reason: {decision.get('reason')}\n"
        f"- requires_new_approval: {decision.get('requires_new_approval')}\n\n"
        "Follow this recovery action without changing the user's task goal. "
        "If SQL is rewritten, run safety checks again; changed write SQL requires a new approval."
    )
    return SystemMessage(content=content)


def _user_error_message(error: ErrorRecord, decision: RecoveryDecision, report_summary: str | None = None) -> AIMessage:
    options = [
        "改为只读诊断",
        "只生成报告",
        "补充权限、连接或审批信息后继续",
        "调整任务范围并重新规划",
    ]
    content = (
        "数据库任务暂时无法自动继续。\n\n"
        f"- 错误类型：{error.get('error_type')}\n"
        f"- 发生步骤：{error.get('step_id') or 'unknown'}\n"
        f"- 原因：{error.get('message')}\n"
        f"- 处理建议：{decision.get('reason')}\n"
        f"- 是否需要新审批：{decision.get('requires_new_approval')}\n\n"
        "可选下一步：\n"
        + "\n".join(f"{idx}. {item}" for idx, item in enumerate(options, start=1))
    )
    if report_summary:
        content += f"\n\n错误报告摘要：{report_summary}"
    return AIMessage(content=content)


def _collaboration_events(error: ErrorRecord, decision: RecoveryDecision) -> list[dict[str, Any]]:
    manager = CollaborationManager({})
    events = [
        manager.event(
            "error_explained",
            f"{error.get('error_type')}: {error.get('message')}",
            step_id=error.get("step_id"),
            payload_ref=error.get("id"),
        ),
        manager.event(
            "repair_attempted",
            f"Recovery action selected: {decision.get('action')} - {decision.get('reason')}",
            step_id=error.get("step_id"),
            payload_ref=decision.get("id"),
        ),
    ]
    if decision.get("action") == "auto_retry":
        events.append(
            manager.event(
                "retry_scheduled",
                f"Retry scheduled for {error.get('error_type')}.",
                step_id=error.get("step_id"),
                payload_ref=decision.get("id"),
            )
        )
    if decision.get("action") in {"ask_user", "abort_safely"}:
        events.append(
            manager.event(
                "user_action_required",
                f"User action required for {error.get('error_type')}.",
                step_id=error.get("step_id"),
                payload_ref=decision.get("id"),
            )
        )
    return events


def _current_step(state: AgentState) -> dict[str, Any]:
    step_id = state.get("current_step_id")
    for step in state.get("task_stack", []) or []:
        if step.get("id") == step_id:
            return step
    return {}


def _has_successful_read_evidence(state: AgentState) -> bool:
    for observation in state.get("db_observations", []) or []:
        payload = observation.get("payload") or {}
        if payload.get("success") is not False:
            return True
    for result in state.get("tool_execution_results", []) or []:
        if result.get("success") is True:
            return True
    return False


def _can_deliver_report_only_after_llm_failure(state: AgentState, error: ErrorRecord) -> bool:
    """Allow structured delivery when only the narrative report model failed.

    Codex/Claude-style loops keep tool evidence and user-facing delivery
    separate: if the database work already collected safe read-only evidence,
    a transient model failure while writing the final prose should not erase
    the result. This does not bypass approval because write/execute phases and
    pending approvals are excluded.
    """
    if error.get("error_type") != "llm_output_error":
        return False
    step = _current_step(state)
    if step.get("phase") != "report":
        return False
    if state.get("pending_approval") or state.get("policy_violation"):
        return False
    return _has_successful_read_evidence(state)


def _mark_current_report_step_completed(state: AgentState) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    step_id = state.get("current_step_id")
    steps = [dict(step) for step in state.get("task_stack", []) or []]
    completed_step: dict[str, Any] | None = None
    for index, step in enumerate(steps):
        if step.get("id") != step_id:
            continue
        step["status"] = "completed"
        step["result"] = "Report generated from structured database evidence after the narrative model timed out."
        step["error"] = None
        steps[index] = step
        completed_step = step
        break
    return steps, completed_step


def error_handler(state: AgentState) -> dict[str, Any]:
    """Classify the active error, select a recovery action, and update state."""
    messages = list(state.get("messages", []))
    error = _active_error(state)
    if not error:
        return {"error": None}

    engine = RecoveryEngine(state)
    recovery_update = engine.update_for_error(error)
    decision = recovery_update["active_recovery_decision"]
    logger.warning(
        "Recovery decision: %s for %s (%s)",
        decision.get("action"),
        error.get("id"),
        error.get("error_type"),
    )

    update: dict[str, Any] = {
        **recovery_update,
        "error": None,
        "collaboration_events": _collaboration_events(error, decision),
    }

    action = decision.get("action")
    if _can_deliver_report_only_after_llm_failure(state, error) and action in {"ask_user", "abort_safely"}:
        steps, completed_step = _mark_current_report_step_completed(state)
        plan = state.get("db_task_plan")
        if plan and completed_step:
            plan = dict(plan)
            plan_steps = [dict(step) for step in plan.get("steps", [])]
            for index, step in enumerate(plan_steps):
                if step.get("id") == completed_step.get("id"):
                    plan_steps[index] = {**step, **completed_step}
                    break
            plan["steps"] = plan_steps
            plan["status"] = "completed"
        report = engine.error_report(
            status="recovered",
            summary=(
                f"{error.get('error_type')}: {error.get('message')}. "
                "数据库只读证据已收集完成，系统将基于结构化证据生成报告。"
            ),
        )
        update["error_reports"] = [report]
        update["retry_count"] = 0
        update["task_stack"] = steps
        if plan:
            update["db_task_plan"] = plan
        update["current_step_id"] = None
        update["loop_status"] = "completed"
    elif action in {"auto_retry", "rewrite_sql", "adjust_tool_args", "run_diagnostic_tool"}:
        update["retry_count"] = int(state.get("retry_count") or 0) + 1
        update["messages"] = messages + [_system_recovery_message(error, decision)]
        update["loop_status"] = "running"
    elif action == "repair_state":
        update["retry_count"] = int(state.get("retry_count") or 0) + 1
        update["messages"] = messages + [_system_recovery_message(error, decision)]
        update["loop_status"] = "replanning"
    elif action == "replan_step":
        update["retry_count"] = 0
        update["messages"] = messages + [_system_recovery_message(error, decision)]
        update["replan_trigger"] = f"error:{error.get('error_type')}"
        update["loop_status"] = "replanning"
    else:
        report = engine.error_report(
            status="failed",
            summary=f"{error.get('error_type')}: {error.get('message')}",
        )
        update["error_reports"] = [report]
        update["retry_count"] = 0
        update["messages"] = messages + [_user_error_message(error, decision, report.get("user_summary"))]
        update["loop_status"] = "blocked"

    next_state = {**state, **update}
    update.update(StateManager(next_state).runtime_update(last_node="error_handler"))
    update.update(StateValidator(next_state).validation_update())
    return update
