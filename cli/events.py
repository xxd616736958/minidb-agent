"""Stable CLI event adapter for LangGraph stream updates."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

from cli.config import mask_database_url

SECRET_KEY_RE = re.compile(r"(password|passwd|secret|token|api[_-]?key|database_url)", re.IGNORECASE)
URL_RE = re.compile(r"postgres(?:ql)?://[^\s\"']+", re.IGNORECASE)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def sanitize_for_cli(value: Any) -> Any:
    """Recursively remove secrets and cap large strings for terminal output."""

    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, raw in value.items():
            if SECRET_KEY_RE.search(str(key)):
                sanitized[str(key)] = "***"
            else:
                sanitized[str(key)] = sanitize_for_cli(raw)
        return sanitized
    if isinstance(value, list):
        return [sanitize_for_cli(item) for item in value[:100]]
    if isinstance(value, tuple):
        return [sanitize_for_cli(item) for item in value[:100]]
    if isinstance(value, str):
        text = URL_RE.sub(lambda match: mask_database_url(match.group(0)) or "***", value)
        return text[:4000] + "...(truncated)" if len(text) > 4000 else text
    return value


class CliEventAdapter:
    """Map internal graph node updates to stable database-task CLI events."""

    def __init__(self, thread_id: str | None = None) -> None:
        self.thread_id = thread_id or "unknown"

    def events_from_stream_data(self, data: dict[str, Any], *, run_id: str | None = None) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        for node_name, output in data.items():
            if node_name in {"__start__", "__interrupt__"}:
                continue
            if not isinstance(output, dict):
                continue
            events.extend(self.events_from_node(node_name, output, run_id=run_id))
        return events

    def events_from_node(self, node_name: str, output: dict[str, Any], *, run_id: str | None = None) -> list[dict[str, Any]]:
        if node_name in {"intent_analyzer", "intent_validator", "clarification_gate"}:
            return [self._event("task_understanding", output, self._intent_summary(output), run_id)]
        if node_name in {"workflow_planner", "task_planner", "delegation_planner"}:
            return [self._event("plan_ready", output, self._plan_summary(output), run_id)]
        if node_name == "tool_policy_gate":
            events = [self._event("safety_check", output, self._safety_summary(output), run_id)]
            if output.get("pending_approval") or output.get("approval_card"):
                events.append(self._event("approval_required", output, self._approval_summary(output), run_id))
            return events
        if node_name == "execute_tools":
            return [self._event("tool_running", output, self._tool_summary(output), run_id)]
        if node_name == "normalize_observation":
            return [self._event("observation_ready", output, self._observation_summary(output), run_id)]
        if node_name == "verify_step":
            return [self._event("verification_ready", output, self._verification_summary(output), run_id)]
        if node_name == "final_report":
            return [self._event("delivery_ready", output, self._delivery_summary(output), run_id)]
        if node_name == "error_handler":
            event_type = "blocked" if output.get("error_reports") else "error"
            return [self._event(event_type, output, self._error_summary(output), run_id)]
        if node_name in {"state_recovery", "step_scheduler", "memory_compactor", "llm_reason"}:
            summary = self._runtime_summary(node_name, output)
            return [self._event("tool_running", output, summary, run_id)] if summary else []
        return []

    def _event(self, event_type: str, payload: dict[str, Any], summary: str, run_id: str | None) -> dict[str, Any]:
        return {
            "type": event_type,
            "thread_id": self.thread_id,
            "run_id": run_id,
            "summary": sanitize_for_cli(summary),
            "payload": sanitize_for_cli(payload),
            "created_at": now_iso(),
        }

    def _intent_summary(self, output: dict[str, Any]) -> str:
        intent = output.get("current_intent") or {}
        card = output.get("task_card") or {}
        pending = output.get("pending_clarification") or {}
        if pending.get("questions"):
            return f"Need clarification: {pending.get('reason') or pending['questions'][0]}"
        return str(intent.get("user_language_summary") or card.get("goal") or "Task understanding updated")

    def _plan_summary(self, output: dict[str, Any]) -> str:
        plan = output.get("db_task_plan") or {}
        steps = plan.get("steps") or output.get("task_stack") or []
        return str(plan.get("summary") or f"Plan ready with {len(steps)} step(s)")

    def _safety_summary(self, output: dict[str, Any]) -> str:
        decisions = output.get("security_policy_decisions") or output.get("tool_policy_decisions") or []
        if decisions:
            latest = decisions[-1]
            return f"Safety decision: {latest.get('decision')} risk={latest.get('risk_level')}"
        return "Safety check updated"

    def _approval_summary(self, output: dict[str, Any]) -> str:
        card = output.get("approval_card") or {}
        approval = output.get("pending_approval") or {}
        risk = card.get("risk_level") or approval.get("risk_level") or "unknown"
        sql_hash = card.get("sql_hash") or approval.get("sql_hash") or "no-sql-hash"
        return f"Approval required risk={risk} sql_hash={sql_hash}"

    def _tool_summary(self, output: dict[str, Any]) -> str:
        records = output.get("tool_invocation_records") or output.get("tool_execution_results") or []
        if records:
            latest = records[-1]
            return f"Tool {latest.get('tool_name') or latest.get('name') or 'unknown'} {latest.get('status') or 'updated'}"
        return "Tool execution updated"

    def _observation_summary(self, output: dict[str, Any]) -> str:
        observations = output.get("db_observations") or []
        if observations:
            latest = observations[-1]
            return str(latest.get("summary") or f"Observation {latest.get('type')}")
        return "Database observation ready"

    def _verification_summary(self, output: dict[str, Any]) -> str:
        results = output.get("verification_results") or []
        if results:
            latest = results[-1]
            return f"Verification {latest.get('status')}: {latest.get('summary')}"
        return "Verification updated"

    def _delivery_summary(self, output: dict[str, Any]) -> str:
        packages = output.get("delivery_packages") or []
        if packages:
            latest = packages[-1]
            return f"Delivery {latest.get('status')}: {latest.get('title') or latest.get('user_report_path')}"
        return "Delivery package ready"

    def _error_summary(self, output: dict[str, Any]) -> str:
        reports = output.get("error_reports") or []
        errors = output.get("error_records") or []
        if reports:
            return str(reports[-1].get("user_summary") or "Task blocked")
        if errors:
            return str(errors[-1].get("message") or "Agent error")
        return "Agent error"

    def _runtime_summary(self, node_name: str, output: dict[str, Any]) -> str:
        runtime = output.get("db_task_runtime") or {}
        if not runtime:
            return ""
        return f"{node_name}: {runtime.get('task_status', 'running')} phase={runtime.get('current_phase') or 'none'}"


def event_to_json(event: dict[str, Any]) -> str:
    return json.dumps(sanitize_for_cli(event), ensure_ascii=False, default=str)
