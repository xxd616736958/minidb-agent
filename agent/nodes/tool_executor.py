"""Tool executor node — runs approved tool calls using LangGraph's ToolNode.

This node uses the official langgraph.prebuilt.ToolNode which handles:
  - Dispatching tool calls to the correct tool instance
  - Parallel execution of multiple tool calls
  - Creating ToolMessage results that update the message history
  - Error handling per tool call
"""

from __future__ import annotations

import logging
from typing import Any

from agent.state import AgentState
from tools.registry import registry

logger = logging.getLogger(__name__)

# Lazy-initialized ToolNode
_tool_node = None


def _get_tool_node():
    """Get or create the ToolNode with current registered tools."""
    global _tool_node
    from langgraph.prebuilt import ToolNode

    tools = registry.get_all()
    # Recreate if tools changed (plugin hot-reload scenario)
    if _tool_node is None or len(_tool_node.tools_by_name) != len(tools):
        _tool_node = ToolNode(tools)
        logger.debug(f"ToolNode initialized with {len(tools)} tools")
    return _tool_node


def execute_tools(state: AgentState) -> dict[str, Any]:
    """Execute approved tool calls from the last LLM message.

    This node:
      1. Extracts tool_calls from the last AIMessage
      2. Dispatches each to the correct tool via ToolNode
      3. Collects results as ToolMessages
      4. Updates task status if we're in a plan

    Returns:
        Partial state with ToolMessages appended and results logged.
    """
    messages = state.get("messages", [])
    if not messages:
        logger.warning("execute_tools called with no messages")
        return {"error": "No messages in state", "step_count": state.get("step_count", 0) + 1}

    last_msg = messages[-1]
    tool_calls = getattr(last_msg, "tool_calls", None)
    if not tool_calls:
        logger.warning("execute_tools called but last message has no tool_calls")
        return {"step_count": state.get("step_count", 0) + 1}

    logger.info(
        f"Executing {len(tool_calls)} tool call(s): "
        f"{[tc['name'] for tc in tool_calls]}"
    )

    # Execute via LangGraph's ToolNode
    try:
        tool_node = _get_tool_node()
        result = tool_node.invoke({"messages": messages})
    except Exception as e:
        logger.error(f"ToolNode execution failed: {e}")
        return {
            "error": f"Tool execution failed: {e}",
            "step_count": state.get("step_count", 0) + 1,
        }

    # Extract results and format for logging
    new_messages = result.get("messages", [])
    tool_results: list[str] = []
    for msg in new_messages:
        if hasattr(msg, "name") and hasattr(msg, "content"):
            content_preview = str(msg.content)[:200]
            tool_results.append(f"[{msg.name}]: {content_preview}")

    # Update task status if executing a plan
    task_stack = list(state.get("task_stack", []))
    current_idx = state.get("current_task_index", 0)
    if task_stack and current_idx < len(task_stack):
        task = dict(task_stack[current_idx])
        if not state.get("error"):
            task["status"] = "completed"
            task["result"] = tool_results[-1] if tool_results else ""
        task_stack[current_idx] = task

    return {
        "messages": new_messages,
        "tool_call_results": tool_results,
        "task_stack": task_stack,
        "step_count": state.get("step_count", 0) + 1,
    }
