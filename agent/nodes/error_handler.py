"""Error handler node — retry, backtrack, and circuit-breaker logic.

Handles errors from any node in the graph:
  - Retryable errors: retry up to max_retries with exponential backoff info
  - Non-retryable errors: pass the error message to the user
  - Timeout errors: retry with increased timeout context
  - Persistent failures: escalate with clear error message
"""

from __future__ import annotations

import logging
from typing import Any

from langchain_core.messages import AIMessage, SystemMessage

from agent.config import get_settings
from agent.state import AgentState

logger = logging.getLogger(__name__)

# ── Error classification ─────────────────────────────────────

RETRYABLE_PATTERNS = [
    "timeout",
    "timed out",
    "connection",
    "rate limit",
    "429",
    "503",
    "502",
    "temporarily",
]

NON_RETRYABLE_PATTERNS = [
    "permission denied",
    "not found",
    "invalid",
    "authentication",
    "400",
    "401",
    "403",
    "api key",
    "quota",
    "tool_calls must be followed",
    "insufficient tool messages",
    "bad request",
]


def _is_retryable(error_msg: str) -> bool:
    """Classify an error as retryable or not based on its message."""
    lower = error_msg.lower()
    for pattern in RETRYABLE_PATTERNS:
        if pattern in lower:
            return True
    for pattern in NON_RETRYABLE_PATTERNS:
        if pattern in lower:
            return False
    # Default: retry transient-looking errors
    return True


# ── Node implementation ──────────────────────────────────────

def error_handler(state: AgentState) -> dict[str, Any]:
    """Error handler node — decides retry, escalate, or abort.

    Called when the `error` field is set on the state.
    Evaluates the error, retry count, and decides the next action.

    Returns:
        Updated state with error cleared (for retry) or a final
        error message for the user.
    """
    settings = get_settings()
    error_msg = state.get("error", "Unknown error")
    retry_count = state.get("retry_count", 0)
    max_retries = state.get("max_retries", settings.max_retries)
    messages = list(state.get("messages", []))

    logger.warning(
        f"Error handler: attempt {retry_count + 1}/{max_retries} — {error_msg[:100]}"
    )

    # Check if retries exhausted
    if retry_count >= max_retries:
        logger.error(f"Max retries ({max_retries}) exhausted: {error_msg}")
        return {
            "error": None,  # Clear error so graph can terminate
            "messages": messages + [
                AIMessage(
                    content=(
                        f"❌ **Failed after {max_retries} retries.**\n\n"
                        f"Error: {error_msg}\n\n"
                        f"Please check your configuration, try a different approach, "
                        f"or simplify your request."
                    )
                ),
            ],
        }

    # Classify error
    if not _is_retryable(error_msg):
        # Non-retryable — report immediately
        logger.error(f"Non-retryable error: {error_msg}")
        return {
            "error": None,
            "messages": messages + [
                AIMessage(
                    content=(
                        f"❌ **Error (non-retryable):** {error_msg}\n\n"
                        f"This error cannot be resolved by retrying. "
                        f"Please check your setup and try again."
                    )
                ),
            ],
        }

    # Retryable — increment counter and add context for LLM
    retry_msg = (
        f"⚠️ Attempt {retry_count + 1} failed: {error_msg}\n"
        f"Retrying (attempt {retry_count + 2} of {max_retries})...\n"
        f"Please try a different approach if possible."
    )

    logger.info(f"Retrying after error (attempt {retry_count + 2}/{max_retries})")

    return {
        "error": None,  # Clear error for retry
        "retry_count": retry_count + 1,
        "messages": messages + [SystemMessage(content=retry_msg)],
    }
