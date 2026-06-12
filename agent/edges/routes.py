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

INTENT_ANALYZER = "intent_analyzer"
INTENT_VALIDATOR = "intent_validator"
CLARIFICATION_GATE = "clarification_gate"
WORKFLOW_PLANNER = "workflow_planner"
STATE_RECOVERY = "state_recovery"
TASK_PLANNER = "task_planner"
STEP_SCHEDULER = "step_scheduler"
MEMORY_COMPACTOR = "memory_compactor"
LLM_REASON = "llm_reason"
TOOL_POLICY_GATE = "tool_policy_gate"
HUMAN_APPROVAL = "human_approval"
EXECUTE_TOOLS = "execute_tools"
NORMALIZE_OBSERVATION = "normalize_observation"
VERIFY_STEP = "verify_step"
ERROR_HANDLER = "error_handler"
END = "__end__"


def route_after_start(
    state: AgentState,
) -> Literal["intent_analyzer", "human_approval"]:
    """Route from START through task understanding before planning.

    Resuming human approval still goes directly to the approval node.
    """
    # Check if we're resuming from a breakpoint
    if state.get("human_interrupt_pending"):
        return HUMAN_APPROVAL
    return INTENT_ANALYZER


def route_after_intent_analyzer(
    state: AgentState,
) -> Literal["intent_validator", "error_handler"]:
    """After intent analysis: validate or handle errors."""
    if state.get("error"):
        return ERROR_HANDLER
    return INTENT_VALIDATOR


def route_after_intent_validator(
    state: AgentState,
) -> Literal["clarification_gate", "workflow_planner", "error_handler"]:
    """After intent validation: ask clarification when required."""
    if state.get("error"):
        return ERROR_HANDLER
    intent = state.get("current_intent")
    if intent and intent.get("requires_clarification"):
        return CLARIFICATION_GATE
    return WORKFLOW_PLANNER


def route_after_clarification(
    state: AgentState,
) -> Literal["workflow_planner", "error_handler", END]:
    """Stop the current turn when waiting for user clarification."""
    if state.get("error"):
        return ERROR_HANDLER
    pending = state.get("pending_clarification")
    if pending and pending.get("status") == "pending":
        return END
    return WORKFLOW_PLANNER


def route_after_workflow_planner(
    state: AgentState,
) -> Literal["task_planner", "error_handler"]:
    """After workflow selection: enter task planning."""
    if state.get("error"):
        return ERROR_HANDLER
    return TASK_PLANNER


def route_after_planner(
    state: AgentState,
) -> Literal["step_scheduler", "memory_compactor", "error_handler"]:
    """After task planning: check for errors, then determine next step."""
    if state.get("error"):
        return ERROR_HANDLER
    if state.get("db_task_plan") or state.get("task_stack"):
        return STEP_SCHEDULER
    return MEMORY_COMPACTOR


def route_after_scheduler(
    state: AgentState,
) -> Literal["memory_compactor", "error_handler", END]:
    """After step scheduling: continue, handle blocked state, or finish."""
    if state.get("error"):
        return ERROR_HANDLER
    if state.get("loop_status") == "completed":
        return END
    if state.get("loop_status") == "blocked":
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
) -> Literal["tool_policy_gate", "verify_step", "error_handler", END]:
    """After LLM response: route based on content.

    - tool_calls → tool_policy_gate
    - error → error_handler
    - planned step without tool calls → step_scheduler after verification-free completion
    - no plan, no tool calls → END
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
        logger.debug(f"LLM produced {len(tool_calls)} tool call(s) → tool_policy_gate")
        return TOOL_POLICY_GATE

    task_stack = state.get("task_stack", [])
    if task_stack and state.get("current_step_id"):
        logger.debug("LLM produced no tools for planned step → verify_step")
        return VERIFY_STEP

    logger.debug("No tool calls, no pending tasks → END (final answer)")
    return END


def route_after_policy_gate(
    state: AgentState,
) -> Literal["human_approval", "llm_reason", "error_handler", END]:
    """After tool policy gate: approve safe calls or ask LLM to adjust."""
    if state.get("error"):
        return ERROR_HANDLER
    pending = state.get("pending_approval")
    if pending and pending.get("status") == "pending":
        return END
    if state.get("policy_violation"):
        return LLM_REASON
    return HUMAN_APPROVAL


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
) -> Literal["normalize_observation", "error_handler"]:
    """After tool execution: check results, handle errors, or continue."""
    if state.get("error"):
        return ERROR_HANDLER
    return NORMALIZE_OBSERVATION


def route_after_observation(
    state: AgentState,
) -> Literal["verify_step", "error_handler"]:
    """After observation normalization: verify current step."""
    if state.get("error"):
        return ERROR_HANDLER
    return VERIFY_STEP


def route_after_verify(
    state: AgentState,
) -> Literal["step_scheduler", "error_handler"]:
    """After step verification: continue or handle blocked state."""
    if state.get("error") or state.get("loop_status") == "blocked":
        return ERROR_HANDLER
    return STEP_SCHEDULER


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
