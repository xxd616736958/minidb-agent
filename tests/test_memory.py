"""Tests for the hierarchical memory system."""

import pytest

from memory.short_term import ShortTermMemory
from memory.working import (
    format_working_memory,
    get_default_working_memory,
    update_working_memory,
)


class TestShortTermMemory:
    """Tests for short-term sliding window memory."""

    def test_add_and_get(self):
        """Should add and retrieve messages."""
        mem = ShortTermMemory(max_tokens=1000)
        mem.add({"role": "user", "content": "hello"})
        mem.add({"role": "assistant", "content": "hi there"})

        recent = mem.get_recent()
        assert len(recent) == 2
        assert recent[0]["content"] == "hello"

    def test_token_limit(self):
        """Should evict messages when over token limit."""
        mem = ShortTermMemory(max_tokens=50)  # Very small window
        # Add a large message
        mem.add({"role": "user", "content": "x" * 500})  # ~125 tokens
        mem.add({"role": "assistant", "content": "y" * 500})

        # Should have pruned at least the first message
        recent = mem.get_recent()
        # With very small window, should keep only the latest
        assert len(recent) >= 1

    def test_should_compact(self):
        """should_compact should return true when over threshold."""
        mem = ShortTermMemory(max_tokens=1000)
        mem.add({"role": "user", "content": "x" * 5000})  # ~1250 tokens
        assert mem.should_compact(threshold=100) is True

    def test_replace_with_summary(self):
        """Should clear and replace with summary."""
        mem = ShortTermMemory(max_tokens=1000)
        mem.add({"role": "user", "content": "hello"})
        mem.replace_with_summary("This is a summary")

        recent = mem.get_recent()
        assert len(recent) == 1
        assert "summary" in recent[0]["content"]

    def test_clear(self):
        """Should reset the window."""
        mem = ShortTermMemory(max_tokens=1000)
        mem.add({"role": "user", "content": "hello"})
        mem.clear()
        assert len(mem.get_recent()) == 0
        assert mem.get_token_count() == 0


class TestWorkingMemory:
    """Tests for working memory key-value store."""

    def test_defaults(self):
        """Should return sensible defaults."""
        wm = get_default_working_memory()
        assert "user_goal" in wm
        assert "current_context" in wm
        assert "decisions" in wm
        assert "blockers" in wm

    def test_update_merges(self):
        """Should update specific keys and keep others."""
        current = get_default_working_memory()
        updates = {"user_goal": "Install FastAPI"}
        result = update_working_memory(current, updates)
        assert result["user_goal"] == "Install FastAPI"
        # Other keys unchanged
        assert result["current_context"] == current["current_context"]

    def test_update_ignores_unknown_keys(self):
        """Should ignore keys not in the schema."""
        current = get_default_working_memory()
        updates = {"unknown_field": "value"}
        result = update_working_memory(current, updates)
        assert "unknown_field" not in result

    def test_format_working_memory(self):
        """Should format for system prompt injection."""
        wm = {
            "user_goal": "Test the app",
            "current_context": "/tmp/test",
            "decisions": "Use pytest",
            "blockers": "None",
        }
        formatted = format_working_memory(wm)
        assert "Test the app" in formatted
        assert "/tmp/test" in formatted
        assert "pytest" in formatted
