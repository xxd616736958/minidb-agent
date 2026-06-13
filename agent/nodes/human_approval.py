"""Human-in-the-loop approval node.

Safety gate that screens tool calls before execution.

Current implementation:
  - Auto-approves all safe tool calls (pass through)
  - Dangerous commands are rejected early with a clear warning message
  - Safety is enforced by ShellTool's whitelist/sandbox as the primary defense

For production HITL with interactive CLI approval:
  - Set interrupt_before=["human_approval"] in graph.py
  - Use interrupt() with the interrupt payload for dangerous commands
  - The CLI client detects interrupts and renders the approval UI
"""

from __future__ import annotations

import logging
import shlex
from typing import Any

from agent.config import get_settings
from agent.state import AgentState
from tools.policy import tool_call_items

logger = logging.getLogger(__name__)


def _detect_dangerous(tool_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Identify dangerous tool calls."""
    settings = get_settings()
    dangerous = []
    for tc in tool_calls:
        if tc.get("name") != "shell_execute":
            continue
        command = tc.get("args", {}).get("command", "")
        if not command:
            continue
        try:
            base = shlex.split(command)[0]
        except ValueError:
            base = command.split()[0] if command.split() else ""
        if base in settings.dangerous_commands_set:
            dangerous.append(tc)
    return dangerous


def human_approval(state: AgentState) -> dict[str, Any]:
    """Screen tool calls before execution.

    Safe commands pass through automatically.
    Dangerous commands are blocked — the LLM receives a rejection
    message and must find an alternative approach.

    For full interactive HITL, set interrupt_before=["human_approval"]
    in graph.py and use LangGraph's interrupt() primitive.
    """
    messages = state.get("messages", [])
    if not messages:
        return {}

    last_msg = messages[-1]
    tool_calls = tool_call_items(last_msg)
    if not tool_calls:
        return {"human_interrupt_pending": False}

    serialized = [
        {"name": tc["name"], "args": tc["args"], "id": tc["id"]}
        for tc in tool_calls
    ]

    dangerous = _detect_dangerous(serialized)

    if not dangerous:
        logger.debug(f"Auto-approved {len(serialized)} safe tool call(s)")
        return {
            "human_interrupt_pending": False,
            "dangerous_command_detected": False,
        }

    # Block dangerous commands — strip them from the message
    dangerous_ids = {tc["id"] for tc in dangerous}
    safe_ids = {tc["id"] for tc in serialized if tc["id"] not in dangerous_ids}

    logger.warning(
        f"Blocked {len(dangerous)} dangerous tool call(s): "
        f"{[tc['args'].get('command', tc['name']) for tc in dangerous]}"
    )

    # Remove dangerous tool calls from the AIMessage
    safe_tool_calls = [tc for tc in tool_calls if tc.get("id") in safe_ids]
    if isinstance(last_msg, dict):
        last_msg["tool_calls"] = safe_tool_calls
        additional = last_msg.get("additional_kwargs")
        if isinstance(additional, dict):
            additional.pop("tool_calls", None)
    else:
        last_msg.tool_calls = safe_tool_calls

    # Add a rejection notice
    from langchain_core.messages import ToolMessage
    rejection_msgs = []
    for tc in dangerous:
        command = tc.get("args", {}).get("command", tc.get("name", "unknown"))
        rejection_msgs.append(
            ToolMessage(
                content=(
                    f"⚠️ BLOCKED: Command '{command}' is in the dangerous commands list "
                    f"and was rejected for safety. Please use an alternative approach "
                    f"or ask the user for explicit permission."
                ),
                tool_call_id=tc["id"],
                name=tc.get("name", "shell_execute"),
            )
        )

    return {
        "messages": [last_msg] + rejection_msgs,
        "human_interrupt_pending": False,
        "dangerous_command_detected": True,
        "tool_calls_pending": [],
    }
