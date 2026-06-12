"""Memory compactor node — summarizes long conversations to prevent context overflow.

When the estimated token count of the message history exceeds the configured
threshold, this node:
  1. Extracts the conversation to summarize (oldest messages)
  2. Calls a lightweight LLM to produce a structured summary
  3. Replaces the summarized messages with a single SystemMessage
  4. Preserves the most recent messages (last N turns)

This is a LangChain-native approach — using an LLM chain for summarization
within the graph node, not a custom implementation.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from agent.llm_factory import create_llm_for_task

from agent.config import get_settings
from agent.state import AgentState
from models.routing import default_model_profiles, fallback_decision_for_error, finish_invocation_record

logger = logging.getLogger(__name__)

# ── Compaction prompt ────────────────────────────────────────

COMPACTION_SYSTEM_PROMPT = """\
Summarize the following conversation excerpt. Your summary must be concise
but preserve ALL of the following:

1. **User's Goal**: What the user is trying to accomplish.
2. **Key Decisions**: Any decisions made or approaches chosen.
3. **Actions Taken**: What commands were executed and their results.
4. **Files Modified**: Which files were read, created, or edited.
5. **Current State**: Where things stand right now — what's done, what's pending.
6. **Errors/Blockers**: Any errors encountered and whether they were resolved.

Format your summary as bullet points. Be specific — include file paths,
command outputs, and error messages where relevant."""

# Number of recent messages to always preserve (not summarized)
KEEP_RECENT_COUNT = 6


# ── Node implementation ──────────────────────────────────────

def _estimate_tokens(messages: list) -> int:
    """Estimate token count from a message list."""
    total = 0
    for m in messages:
        content = str(getattr(m, "content", ""))
        total += max(len(content.encode("utf-8")) // 4, 1)
    return total


def memory_compactor(state: AgentState) -> dict[str, Any]:
    """Long context auto-compression node.

    Checks if the message history exceeds the configured token threshold.
    If so, summarizes older messages into a single SystemMessage,
    preserving the most recent turns intact.

    Returns:
        Partial state with compacted messages if threshold exceeded,
        otherwise an empty dict (no change needed).
    """
    settings = get_settings()
    messages = list(state.get("messages", []))

    if len(messages) < KEEP_RECENT_COUNT + 4:
        # Not enough messages to warrant compaction
        return {}

    estimated_tokens = _estimate_tokens(messages)
    threshold = settings.memory_compact_threshold

    if estimated_tokens < threshold:
        logger.debug(
            f"Compaction not needed: {estimated_tokens} tokens < {threshold}"
        )
        return {}

    logger.info(
        f"Compacting memory: {estimated_tokens} tokens > {threshold} threshold "
        f"({len(messages)} messages)"
    )

    # Split: summarize older messages, keep recent ones
    to_summarize = messages[:-KEEP_RECENT_COUNT]
    to_keep = messages[-KEEP_RECENT_COUNT:]

    # Build summary input
    summary_input = "## Conversation History to Summarize\n\n"
    for m in to_summarize:
        role = getattr(m, "type", "unknown")
        content = str(getattr(m, "content", ""))
        # Include tool call info
        if hasattr(m, "tool_calls") and m.tool_calls:
            for tc in m.tool_calls:
                content += f"\n[Tool call: {tc.get('name', '?')}({tc.get('args', {})})]"
        summary_input += f"[{role}]: {content[:600]}\n\n"

    # Call LLM for summarization (uses a smaller model when available)
    try:
        llm, route, record, profile = create_llm_for_task("memory_compaction", state)
        started_at = time.monotonic()
        summary_response = llm.invoke([
            SystemMessage(content=COMPACTION_SYSTEM_PROMPT),
            HumanMessage(content=summary_input),
        ])
    except Exception as e:
        logger.error(f"Compaction summary failed: {e}")
        # Non-fatal — continue without compaction
        if "route" in locals() and "record" in locals() and "started_at" in locals():
            update = {
                "model_routes": [route],
                "model_invocation_policies": [route["policy"]],
                "model_invocation_records": [
                    finish_invocation_record(record, status="failed", started_at=started_at, error=e, profile=locals().get("profile"))
                ],
                "model_fallback_decisions": [fallback_decision_for_error(route, record, e)],
            }
            if not state.get("model_profiles"):
                update["model_profiles"] = default_model_profiles()
            return update
        return {}

    summary_text = str(summary_response.content) if hasattr(summary_response, "content") else str(summary_response)
    summary_text = summary_text.strip()

    if not summary_text:
        logger.warning("Compaction produced empty summary")
        return {}

    # Build compacted message list:
    # [SystemMessage(summary)] + [most recent messages]
    compacted = [
        SystemMessage(
            content=f"[Conversation Summary — earlier messages summarized]\n\n{summary_text}"
        ),
        *to_keep,
    ]

    new_token_estimate = _estimate_tokens(compacted)
    saved = estimated_tokens - new_token_estimate
    logger.info(
        f"Compaction complete: {len(messages)} → {len(compacted)} messages, "
        f"~{saved} tokens saved"
    )

    update = {
        "messages": compacted,
        "model_routes": [route],
        "model_invocation_policies": [route["policy"]],
        "model_invocation_records": [
            finish_invocation_record(
                record,
                status="succeeded",
                started_at=started_at,
                output_text=summary_text,
                profile=profile,
            )
        ],
    }
    if not state.get("model_profiles"):
        update["model_profiles"] = default_model_profiles()
    return update
