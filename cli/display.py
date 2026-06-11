"""Rich-formatted terminal output for the CLI client.

Handles:
  - Streaming text output with live refresh
  - Tool call display with syntax highlighting
  - Error/warning formatting
  - Approval prompt rendering
  - Progress spinners
"""

from __future__ import annotations

import json
from typing import Any

from rich.console import Console
from rich.json import JSON
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text
from rich.tree import Tree

# Global console instance
console = Console()


def print_banner():
    """Print the CLI welcome banner."""
    banner = Text()
    banner.append("╔══════════════════════════════════════════════════════╗\n", style="bold blue")
    banner.append("║", style="bold blue")
    banner.append("  🖥️  zui xiao agent — Terminal Operating Agent   ", style="bold white")
    banner.append("║\n", style="bold blue")
    banner.append("║", style="bold blue")
    banner.append("  LangGraph • LangChain • LangSmith               ", style="dim cyan")
    banner.append("║\n", style="bold blue")
    banner.append("╚══════════════════════════════════════════════════════╝", style="bold blue")
    console.print(banner)
    console.print()


def print_streaming(text: str):
    """Print streaming text as it arrives."""
    console.print(text, end="", markup=False)


def print_tool_call(tool_name: str, args: dict[str, Any]):
    """Display a tool call with its arguments."""
    panel = Panel(
        JSON(json.dumps(args, indent=2, default=str)),
        title=f"🔧 [bold yellow]{tool_name}[/bold yellow]",
        border_style="yellow",
        padding=(1, 2),
    )
    console.print(panel)


def print_tool_result(tool_name: str, result_preview: str, success: bool = True):
    """Display a tool execution result."""
    style = "green" if success else "red"
    icon = "✅" if success else "❌"

    # Truncate long results
    if len(result_preview) > 500:
        result_preview = result_preview[:500] + "\n... (truncated)"

    panel = Panel(
        result_preview,
        title=f"{icon} [bold {style}]{tool_name}[/bold {style}]",
        border_style=style,
        padding=(1, 1),
    )
    console.print(panel)


def print_thinking(text: str):
    """Display the agent's thinking/reasoning."""
    if text:
        console.print(Markdown(text))


def print_error(message: str):
    """Display an error message."""
    panel = Panel(
        Text(message, style="red"),
        title="❌ Error",
        border_style="red",
        padding=(1, 1),
    )
    console.print(panel)


def print_warning(message: str):
    """Display a warning message."""
    panel = Panel(
        Text(message, style="yellow"),
        title="⚠️  Warning",
        border_style="yellow",
        padding=(1, 1),
    )
    console.print(panel)


def print_plan(plan_text: str):
    """Display the task execution plan."""
    if not plan_text:
        return
    console.print(Markdown(plan_text))


def print_session_info(session_id: str, model: str, tools_count: int):
    """Display session information."""
    table = Table(show_header=False, box=None, padding=(0, 4))
    table.add_column(style="dim")
    table.add_column(style="cyan")
    table.add_row("Session:", session_id[:8] + "..." if len(session_id) > 12 else session_id)
    table.add_row("Model:", model)
    table.add_row("Tools:", str(tools_count))
    console.print(table)
    console.print()


def print_divider():
    """Print a subtle divider."""
    console.print("─" * 60, style="dim")


def print_goodbye():
    """Print exit message."""
    console.print("\n👋 Goodbye!", style="bold")
