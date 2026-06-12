"""Human collaboration protocol builders for PostgreSQL agent workflows."""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Any

from agent.state import (
    AgentState,
    ApprovalCard,
    ApprovalDecision,
    CollaborationEvent,
    DBObservation,
    DBTaskIntent,
    DBTaskPlan,
    PlanReview,
    TaskCard,
    UserFeedback,
    VerificationResult,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _compact(text: Any, limit: int = 220) -> str:
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    return value[:limit]


def _contains_any(text: str, tokens: tuple[str, ...]) -> bool:
    lowered = text.lower()
    for token in tokens:
        if token.isascii():
            if re.search(rf"\b{re.escape(token.lower())}\b", lowered):
                return True
        elif token in text:
            return True
    return False


class CollaborationManager:
    """Build state updates for human-readable collaboration checkpoints."""

    def __init__(self, state: AgentState | None = None) -> None:
        self.state = state or {}

    @staticmethod
    def event(
        event_type: CollaborationEvent["event_type"],
        summary: str,
        *,
        step_id: str | None = None,
        payload_ref: str | None = None,
    ) -> CollaborationEvent:
        return {
            "id": _new_id("collab"),
            "event_type": event_type,
            "step_id": step_id,
            "summary": _compact(summary, 300),
            "payload_ref": payload_ref,
            "created_at": _now_iso(),
        }

    def build_task_card(self, intent: DBTaskIntent) -> TaskCard:
        output_contract = intent.get("output_contract") or {}
        expected_output = (
            output_contract.get("format")
            or output_contract.get("type")
            or output_contract.get("audience")
            or intent.get("suggested_workflow")
            or intent.get("primary_intent")
            or "task result"
        )
        constraints = [
            *list(intent.get("constraints", [])),
            *list(self.state.get("user_constraints", [])),
        ]
        status = "needs_clarification" if intent.get("requires_clarification") or intent.get("missing_slots") else "draft"
        title = intent.get("primary_intent") or intent.get("domain") or "task"
        return {
            "id": _new_id("task-card"),
            "intent_id": str(intent.get("id") or ""),
            "title": str(title).replace("_", " "),
            "goal": str(intent.get("goal") or intent.get("user_language_summary") or ""),
            "target_environment": str(intent.get("target_environment") or "unknown"),
            "target_database": intent.get("target_database"),
            "target_objects": list(intent.get("target_objects", [])),
            "risk_level": str(intent.get("risk_level") or "unknown"),
            "expected_output": str(expected_output),
            "missing_slots": list(intent.get("missing_slots", [])),
            "assumptions": list(intent.get("assumptions", [])),
            "user_constraints": list(dict.fromkeys(str(item) for item in constraints if item)),
            "status": status,
        }

    def task_card_update(self, intent: DBTaskIntent) -> dict[str, Any]:
        card = self.build_task_card(intent)
        event = self.event(
            "task_card_shown",
            f"Task card shown for {card['title']} with risk={card['risk_level']}.",
            payload_ref=card["id"],
        )
        return {
            "task_card": card,
            "collaboration_events": [event],
        }

    def clarification_update(self, request_id: str, questions: list[str]) -> dict[str, Any]:
        summary = questions[0] if questions else "Clarification requested from user."
        return {
            "collaboration_events": [
                self.event(
                    "clarification_requested",
                    summary,
                    payload_ref=request_id,
                )
            ]
        }

    def build_plan_review(self, plan: DBTaskPlan) -> PlanReview:
        status = "pending" if plan.get("requires_user_confirmation") else "approved"
        return {
            "id": _new_id("plan-review"),
            "plan_id": str(plan.get("id") or ""),
            "status": status,
            "reviewed_steps": [str(step.get("id")) for step in plan.get("steps", [])],
            "user_message": (
                "Review the plan before execution because it includes high-risk or approval-gated steps."
                if status == "pending"
                else "Read-only or low-risk plan can proceed unless the user asks to change it."
            ),
            "created_at": _now_iso(),
            "resolved_at": _now_iso() if status == "approved" else None,
        }

    def plan_review_update(self, plan: DBTaskPlan) -> dict[str, Any]:
        review = self.build_plan_review(plan)
        event = self.event(
            "plan_shown",
            f"Plan shown with {len(plan.get('steps', []))} step(s), risk={plan.get('global_risk_level')}.",
            payload_ref=review["id"],
        )
        return {
            "plan_review": review,
            "collaboration_events": [event],
        }

    def _tool_name_from_approval(self, approval: ApprovalDecision) -> str:
        user_message = str(approval.get("user_message") or "")
        match = re.search(r"Tool=([A-Za-z0-9_\-.]+)", user_message)
        if match:
            return match.group(1)
        return "postgres_write"

    def _replay_policy_for_approval(self, approval: ApprovalDecision) -> str:
        sql_hash = approval.get("sql_hash")
        if sql_hash:
            for binding in self.state.get("approval_bindings", []):
                if binding.get("approval_id") == approval.get("id"):
                    return "bound_to_exact_sql_hash"
            return "requires_new_approval_for_changed_sql"
        return "requires_new_approval"

    def build_approval_card(self, approval: ApprovalDecision) -> ApprovalCard:
        db_env = self.state.get("database_environment") or {}
        return {
            "approval_id": str(approval.get("id") or ""),
            "step_id": str(approval.get("step_id") or ""),
            "tool_name": self._tool_name_from_approval(approval),
            "target_environment": str(
                approval.get("target_environment")
                or db_env.get("environment_name")
                or "unknown"
            ),
            "target_database": db_env.get("target_database"),
            "sql_preview": approval.get("sql_preview"),
            "sql_hash": approval.get("sql_hash"),
            "risk_level": str(approval.get("risk_level") or "high"),
            "impact_summary": approval.get("impact_summary"),
            "rollback_summary": approval.get("rollback_summary"),
            "verification_criteria": list(approval.get("verification_criteria", [])),
            "replay_policy": self._replay_policy_for_approval(approval),
            "options": ["approve", "reject", "edit", "dry_run_more", "report_only", "clarify"],
        }

    def approval_card_update(self, approval: ApprovalDecision) -> dict[str, Any]:
        card = self.build_approval_card(approval)
        summary = (
            f"Approval requested for {card['tool_name']} in {card['target_environment']} "
            f"with risk={card['risk_level']}."
        )
        return {
            "approval_card": card,
            "collaboration_events": [
                self.event(
                    "approval_requested",
                    summary,
                    step_id=card["step_id"],
                    payload_ref=card["approval_id"],
                )
            ],
        }

    def tool_call_events(self, decisions: list[dict[str, Any]]) -> list[CollaborationEvent]:
        events: list[CollaborationEvent] = []
        step_id = self.state.get("current_step_id")
        for decision in decisions:
            tool_name = str(decision.get("tool_name") or "tool")
            events.append(
                self.event(
                    "tool_call_shown",
                    f"Tool call {tool_name} evaluated as {decision.get('decision')}.",
                    step_id=str(step_id) if step_id else None,
                    payload_ref=str(decision.get("call_id") or ""),
                )
            )
        return events

    def safety_block_update(
        self,
        *,
        step_id: str | None,
        violation: dict[str, Any] | None,
        decisions: list[dict[str, Any]],
    ) -> dict[str, Any]:
        first_decision = decisions[0] if decisions else {}
        reason = str((violation or {}).get("message") or first_decision.get("reason") or "")
        alternatives = [
            "continue with read-only diagnostics",
            "generate SQL for review without executing it",
            "ask the user to confirm the target environment",
            "produce a report-only recommendation",
        ]
        event = self.event(
            "safety_block_explained",
            f"Safety block explained: {_compact(reason, 180)}. Alternatives: {', '.join(alternatives)}.",
            step_id=step_id,
            payload_ref="policy_violation",
        )
        return {"collaboration_events": [event]}

    def observation_events(self, observations: list[DBObservation]) -> list[CollaborationEvent]:
        if not observations:
            return []
        first = observations[0]
        summary = (
            f"Explained {len(observations)} observation(s); "
            f"latest type={first.get('type')} source={first.get('source_tool')}."
        )
        return [
            self.event(
                "result_explained",
                summary,
                step_id=first.get("step_id"),
                payload_ref=first.get("id"),
            )
        ]

    def verification_event(
        self,
        result: VerificationResult,
        *,
        is_final_report: bool = False,
    ) -> CollaborationEvent:
        return self.event(
            "final_report_shown" if is_final_report else "result_explained",
            result.get("summary") or "Verification result explained.",
            step_id=result.get("step_id"),
            payload_ref=result.get("id"),
        )

    def feedback_from_user_text(
        self,
        content: str,
        *,
        target_ref: str | None = None,
    ) -> UserFeedback | None:
        text = content.strip()
        if not text:
            return None
        feedback_type: UserFeedback["feedback_type"] | None = None
        structured_delta: dict[str, Any] = {}
        should_write_memory = False

        if _contains_any(text, ("停止", "取消", "stop", "cancel", "abort")):
            feedback_type = "stop"
            structured_delta = {"loop_status": "blocked", "replan_trigger": "user_stop"}
        elif _contains_any(text, ("继续", "恢复", "resume", "continue")):
            feedback_type = "resume"
            structured_delta = {"loop_status": "running"}
        elif _contains_any(text, ("只读", "read-only", "read only", "不要执行", "别执行", "不要写", "no write")):
            feedback_type = "constraint"
            structured_delta = {
                "user_constraints": ["只读，不执行数据库写操作"],
                "runtime_policy": {"allow_database_writes": False},
            }
            should_write_memory = True
        elif _contains_any(text, ("只生成报告", "只报告", "report only", "不要执行sql", "不要执行 sql")):
            feedback_type = "preference"
            structured_delta = {
                "user_constraints": ["只生成报告，不执行 SQL"],
                "selected_workflow": "documentation_workflow",
            }
            should_write_memory = True
        elif _contains_any(text, ("改成", "修改", "不是", "correction", "change to")):
            feedback_type = "correction"
            structured_delta = {"replan_trigger": "user_correction"}

        if feedback_type is None:
            return None

        return {
            "id": _new_id("feedback"),
            "feedback_type": feedback_type,
            "target_ref": target_ref,
            "content": _compact(text, 500),
            "structured_delta": structured_delta,
            "should_write_memory": should_write_memory,
            "created_at": _now_iso(),
        }

    def feedback_update_from_text(
        self,
        content: str,
        *,
        target_ref: str | None = None,
    ) -> dict[str, Any]:
        feedback = self.feedback_from_user_text(content, target_ref=target_ref)
        if not feedback:
            return {}

        update: dict[str, Any] = {
            "user_feedback": [feedback],
            "collaboration_events": [
                self.event(
                    "clarification_answered",
                    f"User feedback captured as {feedback['feedback_type']}.",
                    payload_ref=feedback["id"],
                )
            ],
        }
        delta = feedback.get("structured_delta", {})
        constraints = delta.get("user_constraints")
        if constraints:
            existing = list(self.state.get("user_constraints", []))
            update["user_constraints"] = list(dict.fromkeys([*existing, *constraints]))
        runtime_delta = delta.get("runtime_policy")
        if isinstance(runtime_delta, dict):
            runtime = dict(self.state.get("runtime_policy") or {})
            runtime.update(runtime_delta)
            update["runtime_policy"] = runtime
        for key in ("loop_status", "replan_trigger", "selected_workflow"):
            if key in delta:
                update[key] = delta[key]
        return update
