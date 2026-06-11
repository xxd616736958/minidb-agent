"""Memory Manager — orchestrates all three memory tiers.

Responsibilities:
  - Build the complete context string for LLM system prompts
  - Coordinate short-term window pruning with compactor summaries
  - Update working memory from LLM extractions
  - Interface with long-term Store for persistent recall

This is the single entry point for memory operations within graph nodes.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from memory.short_term import ShortTermMemory
from memory.working import (
    format_working_memory,
    get_default_working_memory,
    update_working_memory,
)

logger = logging.getLogger(__name__)


class MemoryManager:
    """Orchestrates short-term, working, and long-term memory.

    Usage in graph nodes:
        manager = MemoryManager(max_window_tokens=8000)
        context = manager.build_context(state)
        # ... inject context into system prompt ...
        manager.update_after_turn(state, llm_response)
    """

    def __init__(self, max_window_tokens: int = 8000):
        self.short_term = ShortTermMemory(max_tokens=max_window_tokens)
        self.max_window_tokens = max_window_tokens

    # ── Context Building ──────────────────────────────────

    def build_context(self, state: dict[str, Any]) -> str:
        """Build the full context string for the LLM system prompt.

        Combines:
          1. Working memory facts (current goals, context, blockers)
          2. Long-term memory references (if any)
          3. Task plan status (if executing a plan)

        Args:
            state: Current AgentState dict.

        Returns:
            A formatted context string ready for injection.
        """
        parts: list[str] = []

        # 1. Working memory
        wm = state.get("working_memory", get_default_working_memory())
        parts.append(format_working_memory(wm))

        # 2. Long-term refs (placeholder — loaded async via Store)
        long_refs = state.get("long_term_refs", [])
        if long_refs:
            parts.append("\n## Relevant Past Context")
            for ref in long_refs[:5]:  # limit to 5
                parts.append(f"- {ref}")

        # 3. Task plan progress
        plan = state.get("plan")
        if plan:
            parts.append(f"\n## Current Plan\n{plan}")

            task_stack = state.get("task_stack", [])
            current_idx = state.get("current_task_index", 0)
            if task_stack:
                parts.append(self._format_task_progress(task_stack, current_idx))

        return "\n".join(parts)

    # ── Post-Turn Updates ─────────────────────────────────

    def update_after_turn(
        self,
        state: dict[str, Any],
        extracted_facts: Optional[dict[str, str]] = None,
    ) -> dict[str, Any]:
        """Update state after an LLM turn.

        Args:
            state: Current AgentState dict.
            extracted_facts: Optional key-value facts extracted by LLM.

        Returns:
            Partial state dict with updated memory fields.
        """
        updates: dict[str, Any] = {}

        # Update working memory
        if extracted_facts:
            current_wm = state.get("working_memory", get_default_working_memory())
            updates["working_memory"] = update_working_memory(current_wm, extracted_facts)

        # Update short-term window with the latest messages
        messages = state.get("messages", [])
        for msg in messages[-5:]:  # last 5 messages
            self.short_term.add({
                "role": getattr(msg, "type", "unknown"),
                "content": str(getattr(msg, "content", ""))[:500],
            })

        return updates

    # ── Compaction ────────────────────────────────────────

    def should_compact(self, state: dict[str, Any], threshold: int) -> bool:
        """Check if memory compaction is needed.

        Estimates total token count from the message history.
        """
        messages = state.get("messages", [])
        estimated = sum(
            len(str(getattr(m, "content", "")).encode("utf-8")) // 4
            for m in messages
        )
        return estimated > threshold

    def build_compaction_context(self, state: dict[str, Any]) -> str:
        """Build the conversation summary for the compactor prompt.

        Extracts the first few and last few messages for context.
        """
        messages = state.get("messages", [])
        if not messages:
            return "No conversation to summarize."

        parts = ["## Conversation to Summarize\n"]

        # First 3 messages (setup context)
        parts.append("### Early Context")
        for m in messages[:3]:
            content = str(getattr(m, "content", ""))[:300]
            parts.append(f"[{getattr(m, 'type', '?')}]: {content}")

        # Last 10 messages (recent context)
        parts.append("\n### Recent Messages")
        for m in messages[-10:]:
            content = str(getattr(m, "content", ""))[:500]
            parts.append(f"[{getattr(m, 'type', '?')}]: {content}")

        return "\n".join(parts)

    # ── Helpers ───────────────────────────────────────────

    @staticmethod
    def _format_task_progress(task_stack: list, current_idx: int) -> str:
        """Format the current task execution progress."""
        lines = ["\n### Task Progress"]
        for i, task in enumerate(task_stack):
            status_icon = {
                "pending": "⬜",
                "running": "🔄",
                "completed": "✅",
                "failed": "❌",
                "skipped": "⏭️",
            }.get(task.get("status", ""), "❓")

            marker = "→ " if i == current_idx else "  "
            deps = f" (depends: {', '.join(task.get('dependencies', []))})" if task.get("dependencies") else ""
            lines.append(f"{marker}{status_icon} [{task.get('id', '?')}] {task.get('description', '')}{deps}")
        return "\n".join(lines)
