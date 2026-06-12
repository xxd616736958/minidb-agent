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


def print_db_plan(plan: dict[str, Any]):
    """Display a structured database task plan."""
    if not plan:
        return

    table = Table(title="Database Task Plan", show_header=True, padding=(0, 1))
    table.add_column("#", style="dim", width=3)
    table.add_column("Step")
    table.add_column("Phase", style="cyan")
    table.add_column("Risk")
    table.add_column("Policy")
    table.add_column("Approval")

    for idx, step in enumerate(plan.get("steps", []), start=1):
        approval = "yes" if step.get("requires_approval") else "no"
        table.add_row(
            str(idx),
            str(step.get("description", ""))[:70],
            str(step.get("phase", "")),
            str(step.get("risk_level", "")),
            str(step.get("tool_policy", "")),
            approval,
        )
    console.print(table)


def print_intent(intent: dict[str, Any]):
    """Display the agent's structured task understanding."""
    if not intent:
        return

    table = Table(title="Task Understanding", show_header=False, padding=(0, 2))
    table.add_column(style="dim")
    table.add_column(style="cyan")
    table.add_row("Domain", str(intent.get("domain", "unknown")))
    table.add_row("Intent", str(intent.get("primary_intent", "unknown")))
    table.add_row("Risk", str(intent.get("risk_level", "unknown")))
    table.add_row("Workflow", str(intent.get("suggested_workflow", "unknown")))
    goal = str(intent.get("goal") or intent.get("user_language_summary") or "")
    if goal:
        table.add_row("Goal", goal[:160])
    missing = intent.get("missing_slots") or []
    if missing:
        table.add_row("Missing", ", ".join(str(item) for item in missing))
    console.print(table)


def print_clarification(request: dict[str, Any]):
    """Display a structured clarification request."""
    if not request:
        return

    questions = request.get("questions") or []
    if not questions:
        return
    body = "\n".join(f"{idx}. {question}" for idx, question in enumerate(questions, start=1))
    panel = Panel(
        body,
        title="Need Clarification",
        border_style="yellow",
        padding=(1, 1),
    )
    console.print(panel)


def print_loop_status(label: str, payload: dict[str, Any]):
    """Display concise Agent Loop status updates."""
    if not payload:
        return
    if label == "state_recovery":
        _print_state_runtime(payload)
        summary = payload.get("recovery_summary")
        if summary:
            console.print(f"[dim]Recovery:[/dim] {summary}")
        _print_integrity(payload)
    elif label == "step_scheduler":
        _print_state_runtime(payload)
        step_id = payload.get("current_step_id")
        status = payload.get("loop_status")
        if step_id:
            console.print(f"[dim]▶ Step:[/dim] [cyan]{step_id}[/cyan] [dim]({status})[/dim]")
        elif status:
            console.print(f"[dim]Loop:[/dim] {status}")
    elif label == "verify_step":
        _print_state_runtime(payload)
        results = payload.get("verification_results") or []
        for result in results:
            console.print(
                f"[dim]✓ Verify:[/dim] [cyan]{result.get('step_id')}[/cyan] "
                f"[dim]{result.get('status')}[/dim]"
            )
    elif label == "normalize_observation":
        observations = payload.get("db_observations") or []
        for obs in observations:
            console.print(
                f"[dim]◦ Observation:[/dim] [cyan]{obs.get('type')}[/cyan] "
                f"{str(obs.get('summary', ''))[:100]}"
            )
    elif label == "tool_policy_gate":
        _print_state_runtime(payload)
        _print_safety(payload)
        _print_pending_approval(payload)
        decisions = payload.get("tool_policy_decisions") or []
        for decision in decisions:
            console.print(
                f"[dim]◦ Tool policy:[/dim] [cyan]{decision.get('tool_name')}[/cyan] "
                f"{decision.get('decision')} - {str(decision.get('reason', ''))[:100]}"
            )
    elif label == "llm_reason":
        _print_state_runtime(payload)
        _print_execution_environment(payload)
        tools = payload.get("available_tools") or []
        if tools:
            console.print(f"[dim]Available tools:[/dim] {', '.join(str(tool) for tool in tools)}")
    elif label == "execute_tools":
        _print_state_runtime(payload)
        _print_execution_environment(payload)
        _print_safety(payload)
        artifacts = payload.get("artifact_records") or []
        for artifact in artifacts:
            console.print(
                f"[dim]↳ Artifact:[/dim] [cyan]{artifact.get('kind')}[/cyan] "
                f"{str(artifact.get('summary', ''))[:100]}"
            )

    written_memories = payload.get("memory_records_written") or []
    for memory in written_memories:
        console.print(
            f"[dim]↳ Memory:[/dim] [cyan]{memory.get('kind')}[/cyan] "
            f"{str(memory.get('summary', ''))[:100]}"
        )


def _print_integrity(payload: dict[str, Any]) -> None:
    reports = payload.get("state_integrity_reports") or []
    if not reports:
        return
    latest = reports[-1]
    if latest.get("ok"):
        return
    errors = latest.get("errors") or []
    warnings = latest.get("warnings") or []
    if errors:
        console.print(f"[dim]State check:[/dim] [red]{str(errors[0])[:120]}[/red]")
    elif warnings:
        console.print(f"[dim]State check:[/dim] [yellow]{str(warnings[0])[:120]}[/yellow]")


def _print_state_runtime(payload: dict[str, Any]) -> None:
    runtime = payload.get("db_task_runtime") or {}
    if not runtime:
        return
    console.print(
        "[dim]State:[/dim] "
        f"[cyan]{runtime.get('task_status', 'unknown')}[/cyan] "
        f"[dim]phase={runtime.get('current_phase') or 'none'} "
        f"step={runtime.get('current_step_id') or 'none'} "
        f"risk={runtime.get('risk_level') or 'unknown'}[/dim]"
    )


def _print_execution_environment(payload: dict[str, Any]) -> None:
    workspace = payload.get("workspace_profile") or {}
    db_env = payload.get("database_environment") or {}
    task_workspace = payload.get("task_workspace") or {}
    if not workspace and not db_env and not task_workspace:
        return

    env_name = db_env.get("environment_name") or "unknown"
    database = db_env.get("target_database") or "unknown-db"
    host = db_env.get("safe_host_label") or "unknown-host"
    access = db_env.get("access_mode") or "unknown"
    task_id = task_workspace.get("task_id") or "no-task"
    root = workspace.get("root_path") or "unknown-workspace"
    console.print(
        "[dim]Execution env:[/dim] "
        f"[cyan]{env_name}/{database}[/cyan] @ {host} "
        f"[dim]access={access} task={task_id} workspace={root}[/dim]"
    )


def _print_safety(payload: dict[str, Any]) -> None:
    decisions = payload.get("security_policy_decisions") or []
    audits = payload.get("safety_audit_records") or []
    if decisions:
        latest = decisions[-1]
        reasons = latest.get("reasons") or []
        reason = str(reasons[0])[:100] if reasons else str(latest.get("decision"))
        console.print(
            "[dim]Safety:[/dim] "
            f"[cyan]{latest.get('scope')}[/cyan] "
            f"{latest.get('subject')} -> {latest.get('decision')} "
            f"[dim]risk={latest.get('risk_level')} {reason}[/dim]"
        )
    if audits:
        latest_audit = audits[-1]
        console.print(
            "[dim]Audit:[/dim] "
            f"[cyan]{latest_audit.get('event_type')}[/cyan] "
            f"{str(latest_audit.get('summary', ''))[:100]}"
        )


def _print_pending_approval(payload: dict[str, Any]) -> None:
    approval = payload.get("pending_approval")
    if not approval:
        return
    sql_hash = approval.get("sql_hash") or "no-sql-hash"
    env = approval.get("target_environment") or "unknown"
    impact = str(approval.get("impact_summary") or "")[:100]
    console.print(
        "[dim]Pending approval:[/dim] "
        f"[cyan]{approval.get('id')}[/cyan] "
        f"[dim]env={env} risk={approval.get('risk_level')} sql_hash={sql_hash} {impact}[/dim]"
    )


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
