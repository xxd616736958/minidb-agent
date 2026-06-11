"""Conditional routing functions — decide the next node after each step.

These functions are attached to graph nodes as conditional edges.
They inspect the current state and return the name of the next node
to execute.
"""

from __future__ import annotations

import logging
from typing import Literal

from agent.state import AgentState

logger = logging.getLogger(__name__)

# ── Node name constants ───────────────────────────────────────

TASK_PLANNER = "task_planner"
MEMORY_COMPACTOR = "memory_compactor"
LLM_REASON = "llm_reason"
HUMAN_APPROVAL = "human_approval"
EXECUTE_TOOLS = "execute_tools"
ERROR_HANDLER = "error_handler"
END = "__end__"


def route_after_start(state: AgentState) -> Literal["task_planner", "llm_reason"]:
    """Route from START: plan complex tasks, or go directly to LLM.

    Always routes to task_planner first, which internally decides
    whether decomposition is needed.
    """
    # Check if we're resuming from a breakpoint
    if state.get("human_interrupt_pending"):
        return HUMAN_APPROVAL
    return TASK_PLANNER


def route_after_planner(
    state: AgentState,
) -> Literal["memory_compactor", "llm_reason", "error_handler"]:
    """After task planning: check for errors, then determine next step."""
    if state.get("error"):
        return ERROR_HANDLER
    return MEMORY_COMPACTOR


def route_after_compactor(
    state: AgentState,
) -> Literal["llm_reason", "error_handler"]:
    """After memory compaction: go to LLM reasoning."""
    if state.get("error"):
        return ERROR_HANDLER
    return LLM_REASON


def route_after_llm(
    state: AgentState,
) -> Literal["human_approval", "error_handler", END]:
    """After LLM response: route based on content.

    - tool_calls → human_approval (to check for dangerous commands)
    - error → error_handler
    - no tool_calls, no error → END (final answer)
    """
    if state.get("error"):
        logger.debug("LLM produced error → error_handler")
        return ERROR_HANDLER

    messages = state.get("messages", [])
    if not messages:
        logger.debug("No messages after LLM → END")
        return END

    last_msg = messages[-1]
    tool_calls = getattr(last_msg, "tool_calls", None)
    if tool_calls:
        logger.debug(f"LLM produced {len(tool_calls)} tool call(s) → human_approval")
        return HUMAN_APPROVAL

    # Check if we have more tasks in the plan
    task_stack = state.get("task_stack", [])
    current_idx = state.get("current_task_index", 0)
    if task_stack and current_idx < len(task_stack):
        # Check if all tasks are done
        pending = [t for t in task_stack if t.get("status") not in ("completed", "failed", "skipped")]
        if pending:
            # Advance to next pending task
            next_idx = task_stack.index(pending[0])
            return LLM_REASON  # Continue execution
        else:
            logger.debug("All tasks complete → END")
            return END

    logger.debug("No tool calls, no pending tasks → END (final answer)")
    return END


def route_after_approval(
    state: AgentState,
) -> Literal["execute_tools", "llm_reason", ERROR_HANDLER, END]:
    """After human approval: execute approved tools or go back to LLM.

    - If tool calls were rejected/edited → may need LLM to try again
    - If approved → execute_tools
    - If error → error_handler
    """
    if state.get("error"):
        return ERROR_HANDLER

    messages = state.get("messages", [])
    if not messages:
        return END

    last_msg = messages[-1]
    tool_calls = getattr(last_msg, "tool_calls", None)

    if tool_calls:
        logger.debug(f"Approval passed → executing {len(tool_calls)} tool call(s)")
        return EXECUTE_TOOLS
    else:
        # Tool calls were rejected/cleared → go back to LLM for alternative
        logger.debug("Tool calls cleared after approval → back to LLM")
        return LLM_REASON


def route_after_tools(
    state: AgentState,
) -> Literal["error_handler", "memory_compactor", END]:
    """After tool execution: check results, handle errors, or continue."""
    if state.get("error"):
        return ERROR_HANDLER

    # Check if we should advance the task
    task_stack = state.get("task_stack", [])
    current_idx = state.get("current_task_index", 0)
    if task_stack and current_idx < len(task_stack):
        # Mark current task as completed, move to next
        next_idx = current_idx + 1
        if next_idx >= len(task_stack):
            return LLM_REASON  # All tasks done, let LLM summarize
        # More tasks → check if next task's dependencies are met
        next_task = task_stack[next_idx]
        deps = next_task.get("dependencies", [])
        completed_ids = {
            t["id"] for t in task_stack
            if t.get("status") == "completed"
        }
        if all(d in completed_ids for d in deps):
            return MEMORY_COMPACTOR  # Continue with next task
        else:
            return LLM_REASON  # Let LLM handle dependency resolution

    # Loop back: compact then LLM again
    return MEMORY_COMPACTOR


def route_after_error_handler(
    state: AgentState,
) -> Literal["llm_reason", END]:
    """After error handling: retry or give up."""
    error = state.get("error")
    if error:
        # Error still present → couldn't recover → END
        return END

    retry_count = state.get("retry_count", 0)
    if retry_count > 0:
        # We retried → go back to LLM
        logger.debug(f"Error handler chose retry (attempt {retry_count}) → llm_reason")
        return LLM_REASON

    # Error cleared but no retry needed (was handled inline)
    return END
