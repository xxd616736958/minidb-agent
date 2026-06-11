"""CLI-based human approval prompt.

When the agent detects a dangerous command, the graph pauses
and this module renders the approval prompt for the user.
"""

from __future__ import annotations

import json
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table
from rich.text import Text

console = Console()


def prompt_human_approval(interrupt_payload: dict[str, Any]) -> dict[str, Any]:
    """Render the human approval prompt and collect the user's decision.

    Args:
        interrupt_payload: The payload from the human_approval node's interrupt() call.
            Contains:
              - type: "approve_command"
              - safe_tool_calls: list of safe tool calls
              - dangerous_tool_calls: list of dangerous tool calls
              - message: human-readable warning

    Returns:
        Decision dict with action and optional modified data.
    """
    console.clear()
    console.print()

    # ── Warning banner ───────────────────────────────────
    warning_text = Text()
    warning_text.append("╔══════════════════════════════════════════════════════╗\n", style="bold red")
    warning_text.append("║", style="bold red")
    warning_text.append("     ⚠️  HUMAN APPROVAL REQUIRED                     ", style="bold yellow")
    warning_text.append("║\n", style="bold red")
    warning_text.append("║", style="bold red")
    warning_text.append("  Dangerous command detected — review before executing  ", style="white")
    warning_text.append("║\n", style="bold red")
    warning_text.append("╚══════════════════════════════════════════════════════╝", style="bold red")
    console.print(warning_text)
    console.print()

    # ── Safe tool calls ──────────────────────────────────
    safe = interrupt_payload.get("safe_tool_calls", [])
    if safe:
        safe_table = Table(title="✅ Safe Tool Calls (auto-approved)", border_style="green")
        safe_table.add_column("Tool", style="cyan")
        safe_table.add_column("Arguments", style="dim")
        for tc in safe:
            safe_table.add_row(
                tc["name"],
                json.dumps(tc["args"], indent=2)[:200],
            )
        console.print(safe_table)
        console.print()

    # ── Dangerous tool calls ─────────────────────────────
    dangerous = interrupt_payload.get("dangerous_tool_calls", [])
    danger_table = Table(title="🚨 Dangerous Tool Calls (review required)", border_style="red")
    danger_table.add_column("#", style="dim")
    danger_table.add_column("Tool", style="bold red")
    danger_table.add_column("Command", style="bold yellow")
    danger_table.add_column("Full Args", style="dim")

    for i, tc in enumerate(dangerous, 1):
        command = tc.get("dangerous_command", tc.get("args", {}).get("command", "?"))
        danger_table.add_row(
            str(i),
            tc["name"],
            command,
            json.dumps(tc.get("args", {}), indent=2)[:300],
        )
    console.print(danger_table)
    console.print()

    # ── Prompt ───────────────────────────────────────────
    console.print()
    console.print("[bold]Options:[/bold]")
    console.print("  [green][a][/green] Approve — execute all commands as-is")
    console.print("  [red][r][/red] Reject — cancel dangerous commands, execute safe ones")
    console.print("  [yellow][e][/yellow] Edit — modify command arguments before execution")
    console.print("  [cyan][m][/cyan] Modify instruction — edit the LLM's request and re-run")
    console.print("  [dim][q][/dim] Quit — abort the entire operation")
    console.print()

    choice = Prompt.ask("Your choice", choices=["a", "r", "e", "m", "q"], default="r")

    if choice == "a":
        return {"action": "approve"}

    elif choice == "r":
        return {"action": "reject"}

    elif choice == "e":
        # Interactive editing of each dangerous tool call
        modified_calls = []
        for tc in dangerous:
            console.print(f"\n[bold]Editing:[/bold] {tc['name']}")
            console.print(f"Current args: {json.dumps(tc['args'], indent=2)}")

            # Focus on the command field for shell_execute
            if tc["name"] == "shell_execute":
                new_command = Prompt.ask(
                    "New command",
                    default=tc["args"].get("command", ""),
                )
                new_args = dict(tc["args"])
                new_args["command"] = new_command
            else:
                new_args_str = Prompt.ask(
                    "New args (JSON)",
                    default=json.dumps(tc["args"]),
                )
                try:
                    new_args = json.loads(new_args_str)
                except json.JSONDecodeError:
                    console.print("[red]Invalid JSON — keeping original args[/red]")
                    new_args = tc["args"]

            modified_calls.append({"id": tc["id"], "args": new_args})

        return {"action": "edit", "modified_calls": modified_calls}

    elif choice == "m":
        # User provides a new instruction → clear tool calls, let LLM regenerate
        console.print("\n[bold]Enter modified instruction:[/bold]")
        modified_content = Prompt.ask("> ")
        return {"action": "edit_and_rerun", "modified_content": modified_content}

    else:
        return {"action": "reject"}
