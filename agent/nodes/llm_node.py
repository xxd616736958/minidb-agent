"""LLM reasoning node — the core intelligence of the agent.

This node:
  1. Builds the system prompt with memory context + tool descriptions
  2. Binds the current step's visible tools to the LLM via bind_tools()
  3. Invokes the LLM with the full message history
  4. Returns the LLM response (may contain tool_calls)
"""

from __future__ import annotations

import logging
from typing import Any

from langchain_core.messages import AIMessage, SystemMessage

from agent.config import get_settings
from agent.context import (
    build_context_snapshot,
    build_db_working_set,
    build_prompt_context,
    retrieve_relevant_memories,
)
from agent.llm_factory import create_llm_with_tools
from agent.state import AgentState
from memory.manager import MemoryManager
from memory.working import WORKING_MEMORY_SYSTEM_PROMPT
from execution.environment import ExecutionEnvironmentManager
from tools.registry import registry

logger = logging.getLogger(__name__)

# ── System prompt template ───────────────────────────────────

SYSTEM_PROMPT_BASE = """\
You are a terminal-operating programming assistant with the ability to execute
shell commands, read and write files, and search code. You help users with
software engineering tasks by reasoning step by step and using tools when needed.

## Capabilities
- Execute shell commands to inspect the system, run code, and manage files
- Read and write files on the local filesystem
- Search codebases with grep-style pattern matching
- Plan and execute multi-step engineering tasks

## Guidelines
1. **Think before acting**: Explain your reasoning before using tools.
2. **One step at a time**: Execute tools sequentially, checking results between steps.
3. **Safety first**: Dangerous commands (rm, sudo, dd) will require human approval.
4. **Be thorough**: Read files before editing them, verify after making changes.
5. **Handle errors**: If a tool returns an error, analyze it and adjust your approach.
6. **Summarize results**: After completing a task, clearly state what was done.

{memory_context}

{database_context}

## Current Environment
- Working directory: {cwd}
- Platform: {platform}
"""


# ── Node implementation ──────────────────────────────────────

def build_system_prompt(state: dict[str, Any]) -> str:
    """Construct the system prompt with full memory context."""
    import os
    import platform

    settings = get_settings()
    manager = MemoryManager(max_window_tokens=settings.memory_window_tokens)

    memory_context = manager.build_context(state)
    database_context, _ = build_prompt_context(state)

    return SYSTEM_PROMPT_BASE.format(
        memory_context=memory_context,
        database_context=database_context,
        cwd=os.getcwd(),
        platform=platform.platform(),
    ) + "\n" + WORKING_MEMORY_SYSTEM_PROMPT


def _sanitize_tool_call_messages(messages: list) -> list:
    """Strip orphaned tool_calls from AIMessages that have no matching ToolMessages.

    DeepSeek (and some OpenAI-compatible APIs) strictly require that every
    assistant message with tool_calls is followed by tool messages responding
    to each tool_call_id. If a previous run failed mid-execution, the checkpoint
    may contain AIMessages with unresolved tool_calls — this function strips them.
    """
    if not messages:
        return messages

    cleaned = []
    for i, msg in enumerate(messages):
        tool_calls = getattr(msg, "tool_calls", None)
        if not tool_calls:
            cleaned.append(msg)
            continue

        # Check if the following messages contain matching ToolMessages
        tool_call_ids = {tc["id"] for tc in tool_calls if isinstance(tc, dict) and "id" in tc}
        if not tool_call_ids:
            cleaned.append(msg)
            continue

        # Look ahead for matching tool responses
        matched = False
        for j in range(i + 1, min(i + 10, len(messages))):
            later = messages[j]
            tc_id = getattr(later, "tool_call_id", None)
            if tc_id and tc_id in tool_call_ids:
                matched = True
                break

        if matched:
            cleaned.append(msg)
        else:
            # Orphaned tool_calls — strip them via a new message
            logger.warning(
                f"Stripping orphaned tool_calls from message {i}: "
                f"{[tc.get('name','?') if isinstance(tc,dict) else '?' for tc in tool_calls]}"
            )
            from langchain_core.messages import AIMessage
            cleaned.append(AIMessage(
                content=getattr(msg, "content", "") or "[Tool calls were cancelled due to a previous error]",
                tool_calls=[],
            ))

    return cleaned


def llm_reason(state: AgentState) -> dict[str, Any]:
    """LLM reasoning node — the core decision-making step.

    Called after memory compaction (if needed) or task planning.
    The LLM receives:
      - System prompt with full memory context + tool descriptions
      - Complete message history
      - Dynamically allowed tool definitions (via bind_tools)

    Returns:
        Partial state with the new AIMessage appended.
    """
    settings = get_settings()
    logger.info(f"LLM reasoning step (session={state.get('session_id', '?')})")

    # Build conversation
    messages = list(state.get("messages", []))

    # Sanitize messages for DeepSeek compatibility:
    # DeepSeek requires every AIMessage with tool_calls to be followed
    # by matching ToolMessages. Strip orphaned tool_calls from history.
    messages = _sanitize_tool_call_messages(messages)

    step_context = None
    environment_update = ExecutionEnvironmentManager(state).bootstrap_state()
    state_with_environment = {**state, **environment_update}
    db_working_set = build_db_working_set(state_with_environment)
    retrieved_memories = retrieve_relevant_memories({**state_with_environment, "db_working_set": db_working_set})
    enriched_state = {**state_with_environment, "db_working_set": db_working_set, "retrieved_memories": retrieved_memories}
    visible_tools, visible_specs = registry.get_for_state(enriched_state)
    try:
        llm = create_llm_with_tools(visible_tools)
    except Exception as e:
        logger.error(f"Failed to create LLM: {e}")
        return {
            "error": f"LLM initialization failed: {e}",
            "step_count": state.get("step_count", 0) + 1,
        }
    context_snapshot = build_context_snapshot(enriched_state)
    _, step_context = build_prompt_context(enriched_state)

    # Prepend system prompt after database context and memory retrieval have
    # been normalized so the LLM sees the same context stored in state.
    system_content = build_system_prompt(enriched_state)
    if messages and isinstance(messages[0], SystemMessage):
        messages[0] = SystemMessage(content=system_content)
    else:
        messages.insert(0, SystemMessage(content=system_content))

    # Invoke LLM
    try:
        response = llm.invoke(messages)
    except Exception as e:
        logger.error(f"LLM invocation failed: {e}")
        return {
            "error": f"LLM call failed: {e}",
            "step_count": state.get("step_count", 0) + 1,
        }

    logger.info(
        f"LLM response: {len(response.content)} chars, "
        f"{len(response.tool_calls) if response.tool_calls else 0} tool calls"
    )

    # Check for tool calls → set flags
    has_tool_calls = bool(response.tool_calls)

    return {
        "messages": [response],
        "step_count": state.get("step_count", 0) + 1,
        "step_context": step_context,
        "db_working_set": db_working_set,
        "retrieved_memories": retrieved_memories,
        **environment_update,
        "available_tools": [tool.name for tool in visible_tools],
        "available_tool_specs": visible_specs,
        "context_snapshots": [context_snapshot],
        "tool_calls_pending": (
            [
                {"name": tc["name"], "args": tc["args"], "id": tc["id"]}
                for tc in response.tool_calls
            ]
            if has_tool_calls else []
        ),
    }
