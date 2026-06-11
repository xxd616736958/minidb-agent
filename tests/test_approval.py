"""Tests for the human-in-the-loop approval system."""

import pytest

from agent.nodes.human_approval import (
    _detect_dangerous,
    human_approval,
)


class TestDangerousDetection:
    """Tests for dangerous command detection."""

    def test_safe_ls_not_dangerous(self):
        """ls should not be flagged as dangerous."""
        tcs = [
            {"name": "shell_execute", "args": {"command": "ls -la"}, "id": "1"},
        ]
        assert _detect_dangerous(tcs) == []

    def test_rm_is_dangerous(self):
        """rm should be flagged."""
        tcs = [
            {"name": "shell_execute", "args": {"command": "rm -rf /tmp/test"}, "id": "1"},
        ]
        dangerous = _detect_dangerous(tcs)
        assert len(dangerous) == 1
        assert dangerous[0]["id"] == "1"

    def test_sudo_is_dangerous(self):
        """sudo should be flagged."""
        tcs = [
            {"name": "shell_execute", "args": {"command": "sudo systemctl restart nginx"}, "id": "1"},
        ]
        assert len(_detect_dangerous(tcs)) == 1

    def test_non_shell_tool_not_dangerous(self):
        """Non-shell tools are never dangerous."""
        tcs = [
            {"name": "file_read", "args": {"path": "/etc/passwd"}, "id": "1"},
        ]
        assert _detect_dangerous(tcs) == []

    def test_empty_command_not_dangerous(self):
        """Empty command should not crash."""
        tcs = [
            {"name": "shell_execute", "args": {}, "id": "1"},
        ]
        assert _detect_dangerous(tcs) == []

    def test_mixed_safe_and_dangerous(self):
        """Mixed: only dangerous flagged."""
        tcs = [
            {"name": "shell_execute", "args": {"command": "ls"}, "id": "1"},
            {"name": "shell_execute", "args": {"command": "rm -rf /"}, "id": "2"},
            {"name": "file_read", "args": {"path": "test.py"}, "id": "3"},
        ]
        dangerous = _detect_dangerous(tcs)
        assert len(dangerous) == 1
        assert dangerous[0]["id"] == "2"

    def test_all_dangerous(self):
        """All dangerous → all flagged."""
        tcs = [
            {"name": "shell_execute", "args": {"command": "sudo rm -rf /"}, "id": "1"},
            {"name": "shell_execute", "args": {"command": "dd if=/dev/zero of=/dev/sda"}, "id": "2"},
        ]
        assert len(_detect_dangerous(tcs)) == 2


class TestHumanApproval:
    """Tests for the human_approval node."""

    def _make_state(self, tool_calls=None):
        from langchain_core.messages import AIMessage
        msg = AIMessage(
            content="",
            tool_calls=tool_calls or [],
        )
        return {
            "messages": [msg],
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

    def test_safe_commands_pass_through(self):
        """Safe commands should be auto-approved."""
        tcs = [
            {"name": "shell_execute", "args": {"command": "ls"}, "id": "1"},
        ]
        state = self._make_state(tcs)
        result = human_approval(state)
        assert result["human_interrupt_pending"] is False
        assert result["dangerous_command_detected"] is False

    def test_dangerous_commands_blocked(self):
        """Dangerous commands should be blocked with rejection messages."""
        tcs = [
            {"name": "shell_execute", "args": {"command": "sudo rm -rf /"}, "id": "1"},
        ]
        state = self._make_state(tcs)
        result = human_approval(state)
        assert result["dangerous_command_detected"] is True
        assert len(result.get("messages", [])) > 1  # Should have rejection ToolMessages

    def test_no_tool_calls_passes(self):
        """No tool calls should return empty."""
        state = self._make_state(None)
        state["messages"] = []
        result = human_approval(state)
        assert result == {}

    def test_mixed_keeps_safe_blocks_dangerous(self):
        """Safe commands survive, dangerous ones are stripped."""
        tcs = [
            {"name": "file_read", "args": {"path": "test.py"}, "id": "1"},
            {"name": "shell_execute", "args": {"command": "sudo rm -rf /"}, "id": "2"},
        ]
        state = self._make_state(tcs)
        result = human_approval(state)
        assert result["dangerous_command_detected"] is True
        # The safe file_read should remain in the last message's tool_calls
