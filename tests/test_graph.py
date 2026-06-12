"""Tests for the LangGraph state machine."""

import pytest

from agent.state import AgentState, TaskStep
from agent.edges.routes import (
    route_after_llm,
    route_after_delegation_planner,
    route_after_planner,
    route_after_start,
    route_after_intent_validator,
    route_after_clarification,
    route_after_policy_gate,
    route_after_tools,
    route_after_error_handler,
    END,
    INTENT_ANALYZER,
    CLARIFICATION_GATE,
    WORKFLOW_PLANNER,
    TOOL_POLICY_GATE,
    LLM_REASON,
    STATE_RECOVERY,
    TASK_PLANNER,
    HUMAN_APPROVAL,
    EXECUTE_TOOLS,
    ERROR_HANDLER,
    FINAL_REPORT,
    MEMORY_COMPACTOR,
    DELEGATION_PLANNER,
    STEP_SCHEDULER,
)


class TestAgentState:
    """Tests for the AgentState schema."""

    def test_initial_state(self):
        """Should create a valid initial state."""
        state: AgentState = {
            "messages": [],
            "is_last_step": False,
            "plan": None,
            "task_stack": [],
            "current_task_index": 0,
            "tool_calls_pending": [],
            "tool_call_results": [],
            "dangerous_command_detected": False,
            "human_interrupt_pending": False,
            "human_interrupt_type": None,
            "human_interrupt_payload": None,
            "error": None,
            "retry_count": 0,
            "max_retries": 3,
            "step_count": 0,
            "session_id": "test-session",
            "short_term": [],
            "working_memory": {},
            "long_term_refs": [],
        }
        assert state["session_id"] == "test-session"
        assert state["max_retries"] == 3
        assert state["step_count"] == 0


class TestTaskStep:
    """Tests for the TaskStep schema."""

    def test_task_step_creation(self):
        task: TaskStep = {
            "id": "test-1",
            "description": "Run tests",
            "status": "pending",
            "dependencies": [],
            "result": None,
            "error": None,
        }
        assert task["id"] == "test-1"
        assert task["status"] == "pending"

    def test_task_step_with_deps(self):
        task: TaskStep = {
            "id": "test-2",
            "description": "Deploy after tests pass",
            "status": "pending",
            "dependencies": ["test-1"],
            "result": None,
            "error": None,
        }
        assert "test-1" in task["dependencies"]


class TestRouting:
    """Tests for conditional routing functions."""

    def _make_state(self, **overrides) -> AgentState:
        """Create a minimal state for testing routes."""
        state: AgentState = {
            "messages": [],
            "is_last_step": False,
            "plan": None,
            "task_stack": [],
            "current_task_index": 0,
            "tool_calls_pending": [],
            "tool_call_results": [],
            "dangerous_command_detected": False,
            "human_interrupt_pending": False,
            "human_interrupt_type": None,
            "human_interrupt_payload": None,
            "error": None,
            "retry_count": 0,
            "max_retries": 3,
            "step_count": 0,
            "session_id": "test",
            "short_term": [],
            "working_memory": {},
            "long_term_refs": [],
        }
        state.update(overrides)  # type: ignore
        return state

    def test_route_after_llm_no_tool_calls(self):
        """No tool calls + no error → final_report."""
        from langchain_core.messages import AIMessage
        state = self._make_state(
            messages=[AIMessage(content="Hello!")],
        )
        assert route_after_llm(state) == FINAL_REPORT

    def test_route_after_llm_with_tool_calls(self):
        """Tool calls → tool_policy_gate."""
        from langchain_core.messages import AIMessage
        msg = AIMessage(content="", tool_calls=[{"name": "shell_execute", "args": {"command": "ls"}, "id": "1"}])
        state = self._make_state(messages=[msg])
        assert route_after_llm(state) == TOOL_POLICY_GATE

    def test_route_after_llm_with_error(self):
        """Error → error_handler."""
        state = self._make_state(error="Something went wrong")
        assert route_after_llm(state) == ERROR_HANDLER

    def test_route_after_start_enters_intent_analyzer(self):
        """New turns should start with task understanding."""
        state = self._make_state()
        assert route_after_start(state) == INTENT_ANALYZER

    def test_route_after_start_resumes_human_approval(self):
        """Approval resumes still bypass task understanding."""
        state = self._make_state(human_interrupt_pending=True)
        assert route_after_start(state) == HUMAN_APPROVAL

    def test_route_after_intent_validator_clarifies_when_needed(self):
        """Missing slots should route to clarification."""
        state = self._make_state(
            current_intent={
                "requires_clarification": True,
            }
        )
        assert route_after_intent_validator(state) == CLARIFICATION_GATE

    def test_route_after_clarification_stops_when_pending(self):
        """Pending clarification should end the current turn."""
        state = self._make_state(
            pending_clarification={
                "id": "1",
                "questions": ["Which environment?"],
                "missing_slots": ["target_environment"],
                "reason": "missing environment",
                "status": "pending",
            }
        )
        assert route_after_clarification(state) == END

    def test_route_after_policy_gate_allows_to_approval(self):
        state = self._make_state(policy_violation=None)
        assert route_after_policy_gate(state) == HUMAN_APPROVAL

    def test_route_after_policy_gate_violation_returns_to_llm(self):
        state = self._make_state(policy_violation={"message": "blocked"})
        assert route_after_policy_gate(state) == LLM_REASON

    def test_route_after_policy_gate_stops_for_pending_approval(self):
        state = self._make_state(
            policy_violation={"message": "approval required"},
            pending_approval={
                "id": "approval-1",
                "step_id": "execute",
                "status": "pending",
                "risk_level": "high",
                "target_environment": "dev",
                "sql_preview": "UPDATE orders SET status='done' WHERE id=1",
                "sql_hash": "abc12345",
                "impact_summary": "Update one row",
                "rollback_summary": "Restore old status",
                "user_message": None,
                "created_at": "now",
                "resolved_at": None,
            },
        )
        assert route_after_policy_gate(state) == END

    def test_route_after_planner_no_error(self):
        """No error → memory_compactor."""
        state = self._make_state()
        assert route_after_planner(state) == MEMORY_COMPACTOR

    def test_route_after_planner_with_plan_enters_delegation_planner(self):
        """Planned tasks should pass through delegation planning before scheduling."""
        state = self._make_state(task_stack=[{"id": "observe", "status": "pending"}])
        assert route_after_planner(state) == DELEGATION_PLANNER

    def test_route_after_delegation_planner_enters_scheduler(self):
        state = self._make_state()
        assert route_after_delegation_planner(state) == STEP_SCHEDULER

    def test_route_after_planner_with_error(self):
        """Error → error_handler."""
        state = self._make_state(error="Plan failed")
        assert route_after_planner(state) == ERROR_HANDLER

    def test_route_after_error_handler_retry(self):
        """Should retry when retry_count < max_retries."""
        state = self._make_state(retry_count=1, max_retries=3)
        # Error cleared (handler should do this), but retry count set → retry
        result = route_after_error_handler(state)
        assert result == LLM_REASON

    def test_route_after_error_handler_uses_structured_next_node(self):
        state = self._make_state(
            active_recovery_decision={
                "id": "recovery-1",
                "error_id": "err-1",
                "action": "replan_step",
                "reason": "Need replanning",
                "confidence": 0.8,
                "safety_notes": [],
                "requires_new_approval": False,
                "next_node": TASK_PLANNER,
                "created_at": "now",
            }
        )
        assert route_after_error_handler(state) == TASK_PLANNER

        state["active_recovery_decision"]["next_node"] = STATE_RECOVERY
        assert route_after_error_handler(state) == STATE_RECOVERY

    def test_route_after_error_handler_no_retry_needed(self):
        """No error, no retry count → final_report."""
        state = self._make_state(retry_count=0)
        assert route_after_error_handler(state) == FINAL_REPORT
