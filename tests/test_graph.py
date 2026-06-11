"""Tests for the LangGraph state machine."""

import pytest

from agent.state import AgentState, TaskStep
from agent.edges.routes import (
    route_after_llm,
    route_after_planner,
    route_after_tools,
    route_after_error_handler,
    END,
    LLM_REASON,
    HUMAN_APPROVAL,
    EXECUTE_TOOLS,
    ERROR_HANDLER,
    MEMORY_COMPACTOR,
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
        """No tool calls + no error → END."""
        from langchain_core.messages import AIMessage
        state = self._make_state(
            messages=[AIMessage(content="Hello!")],
        )
        assert route_after_llm(state) == END

    def test_route_after_llm_with_tool_calls(self):
        """Tool calls → human_approval."""
        from langchain_core.messages import AIMessage
        msg = AIMessage(content="", tool_calls=[{"name": "shell_execute", "args": {"command": "ls"}, "id": "1"}])
        state = self._make_state(messages=[msg])
        assert route_after_llm(state) == HUMAN_APPROVAL

    def test_route_after_llm_with_error(self):
        """Error → error_handler."""
        state = self._make_state(error="Something went wrong")
        assert route_after_llm(state) == ERROR_HANDLER

    def test_route_after_planner_no_error(self):
        """No error → memory_compactor."""
        state = self._make_state()
        assert route_after_planner(state) == MEMORY_COMPACTOR

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

    def test_route_after_error_handler_no_retry_needed(self):
        """No error, no retry count → END."""
        state = self._make_state(retry_count=0)
        assert route_after_error_handler(state) == END
