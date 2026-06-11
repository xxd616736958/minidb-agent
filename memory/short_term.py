"""Short-term memory: in-memory sliding window of recent messages.

Manages a bounded token window of the most recent conversation turns.
When the window exceeds the token limit, oldest messages are evicted.
The memory compactor node handles summarization before eviction.
"""

from __future__ import annotations

import logging
from collections import deque
from typing import Any, Optional

logger = logging.getLogger(__name__)


class ShortTermMemory:
    """Bounded sliding window of recent message excerpts.

    Not persisted directly — the full message history lives in LangGraph
    checkpoint state. This is an in-memory acceleration structure for
    the compactor to track what's "hot."
    """

    def __init__(self, max_tokens: int = 8000):
        self.max_tokens = max_tokens
        self._window: deque[dict[str, Any]] = deque()
        self._estimated_tokens: int = 0

    def add(self, message: dict[str, Any]) -> None:
        """Add a message excerpt to the window."""
        self._window.append(message)
        self._estimated_tokens += self._estimate_message_tokens(message)
        self._prune()

    def add_many(self, messages: list[dict[str, Any]]) -> None:
        """Add multiple messages at once."""
        for msg in messages:
            self.add(msg)

    def get_recent(self) -> list[dict[str, Any]]:
        """Return the current window contents."""
        return list(self._window)

    def get_token_count(self) -> int:
        """Return the estimated token count of the window."""
        return self._estimated_tokens

    def should_compact(self, threshold: int) -> bool:
        """Return True if the window exceeds the compaction threshold."""
        return self._estimated_tokens > threshold

    def clear(self) -> None:
        """Reset the short-term window."""
        self._window.clear()
        self._estimated_tokens = 0

    def replace_with_summary(self, summary: str) -> None:
        """Replace all messages with a single summary message."""
        self.clear()
        self.add({"role": "system", "content": f"[Conversation summary]\n{summary}"})

    # ── Private ────────────────────────────────────────────

    def _prune(self) -> None:
        """Remove oldest messages until under the token limit."""
        while self._estimated_tokens > self.max_tokens and len(self._window) > 1:
            old = self._window.popleft()
            self._estimated_tokens -= self._estimate_message_tokens(old)
            logger.debug(f"Evicted message from short-term window "
                         f"(tokens remaining: {self._estimated_tokens})")

    @staticmethod
    def _estimate_message_tokens(message: dict[str, Any]) -> int:
        """Rough token estimate: ~4 chars per token."""
        content = str(message.get("content", ""))
        return max(len(content) // 4, 1)
