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


def print_task_card(card: dict[str, Any]):
    """Display the human-facing task card."""
    if not card:
        return

    table = Table(title="Task Card", show_header=False, padding=(0, 2))
    table.add_column(style="dim")
    table.add_column(style="cyan")
    table.add_row("Status", str(card.get("status", "draft")))
    table.add_row("Goal", str(card.get("goal", ""))[:180])
    table.add_row("Environment", str(card.get("target_environment", "unknown")))
    table.add_row("Database", str(card.get("target_database") or "unknown"))
    table.add_row("Risk", str(card.get("risk_level", "unknown")))
    table.add_row("Expected Output", str(card.get("expected_output", ""))[:120])
    missing = card.get("missing_slots") or []
    if missing:
        table.add_row("Missing", ", ".join(str(item) for item in missing))
    constraints = card.get("user_constraints") or []
    if constraints:
        table.add_row("Constraints", "; ".join(str(item) for item in constraints[:3]))
    console.print(table)


def print_plan_review(review: dict[str, Any]):
    """Display the human-facing plan review state."""
    if not review:
        return

    status = str(review.get("status", "pending"))
    style = "yellow" if status == "pending" else "green"
    body = [
        f"Plan: {review.get('plan_id')}",
        f"Status: {status}",
        f"Reviewed steps: {', '.join(str(item) for item in (review.get('reviewed_steps') or [])[:8])}",
    ]
    if review.get("user_message"):
        body.append(str(review["user_message"]))
    console.print(
        Panel(
            "\n".join(body),
            title="Plan Review",
            border_style=style,
            padding=(1, 1),
        )
    )


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
        print_approval_card(payload.get("approval_card") or {})
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
    elif label == "error_handler":
        _print_state_runtime(payload)
    elif label == "final_report":
        _print_state_runtime(payload)
        _print_delivery(payload)

    written_memories = payload.get("memory_records_written") or []
    for memory in written_memories:
        console.print(
            f"[dim]↳ Memory:[/dim] [cyan]{memory.get('kind')}[/cyan] "
            f"{str(memory.get('summary', ''))[:100]}"
        )

    _print_error_recovery(payload)
    _print_delegation(payload)
    _print_model_routing(payload)
    _print_delivery(payload)
    _print_quality(payload)
    _print_collaboration_events(payload)


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


def print_approval_card(card: dict[str, Any]) -> None:
    """Display a database-specific approval card."""
    if not card:
        return

    table = Table(title="Database Approval Card", show_header=False, padding=(0, 2))
    table.add_column(style="dim")
    table.add_column(style="cyan")
    table.add_row("Approval", str(card.get("approval_id", "")))
    table.add_row("Tool", str(card.get("tool_name", "")))
    table.add_row("Environment", str(card.get("target_environment", "unknown")))
    table.add_row("Database", str(card.get("target_database") or "unknown"))
    table.add_row("Risk", str(card.get("risk_level", "high")))
    table.add_row("SQL Hash", str(card.get("sql_hash") or "no-sql-hash"))
    table.add_row("Replay", str(card.get("replay_policy", "")))
    if card.get("impact_summary"):
        table.add_row("Impact", str(card.get("impact_summary"))[:140])
    if card.get("rollback_summary"):
        table.add_row("Rollback", str(card.get("rollback_summary"))[:140])
    options = card.get("options") or []
    if options:
        table.add_row("Options", ", ".join(str(item) for item in options))
    console.print(table)


def _print_collaboration_events(payload: dict[str, Any]) -> None:
    events = payload.get("collaboration_events") or []
    if not events:
        return
    latest = events[-1]
    console.print(
        "[dim]Collaboration:[/dim] "
        f"[cyan]{latest.get('event_type')}[/cyan] "
        f"{str(latest.get('summary', ''))[:120]}"
    )


def _print_error_recovery(payload: dict[str, Any]) -> None:
    errors = payload.get("error_records") or []
    decisions = payload.get("recovery_decisions") or []
    budgets = payload.get("retry_budgets") or []
    reports = payload.get("error_reports") or []
    if errors:
        latest = errors[-1]
        console.print(
            "[dim]Error:[/dim] "
            f"[cyan]{latest.get('error_type')}[/cyan] "
            f"step={latest.get('step_id') or 'none'} "
            f"tool={latest.get('tool_name') or 'none'} "
            f"{str(latest.get('message', ''))[:100]}"
        )
    if decisions:
        latest_decision = decisions[-1]
        console.print(
            "[dim]Recovery:[/dim] "
            f"[cyan]{latest_decision.get('action')}[/cyan] "
            f"{str(latest_decision.get('reason', ''))[:120]}"
        )
    if budgets:
        latest_budget = budgets[-1]
        console.print(
            "[dim]Retry budget:[/dim] "
            f"{latest_budget.get('attempts')}/{latest_budget.get('max_attempts')} "
            f"exhausted={latest_budget.get('exhausted')}"
        )
    if reports:
        latest_report = reports[-1]
        console.print(
            "[dim]Error report:[/dim] "
            f"[cyan]{latest_report.get('status')}[/cyan] "
            f"{str(latest_report.get('user_summary', ''))[:120]}"
        )


def _print_quality(payload: dict[str, Any]) -> None:
    gates = payload.get("quality_gates") or []
    evaluations = payload.get("evaluation_results") or []
    reports = payload.get("quality_reports") or []
    if gates:
        latest = gates[-1]
        failed = latest.get("failed_checks") or []
        console.print(
            "[dim]Quality gate:[/dim] "
            f"[cyan]{latest.get('gate_type')}[/cyan] "
            f"{latest.get('status')} blocking={latest.get('blocking')} "
            f"{str(failed[:2])[:100]}"
        )
    if evaluations:
        latest_eval = evaluations[-1]
        console.print(
            "[dim]Evaluation:[/dim] "
            f"[cyan]{latest_eval.get('case_id')}[/cyan] "
            f"{latest_eval.get('status')} "
            f"{str(latest_eval.get('summary', ''))[:100]}"
        )
    if reports:
        latest_report = reports[-1]
        console.print(
            "[dim]Quality report:[/dim] "
            f"[cyan]{latest_report.get('scope')}[/cyan] "
            f"{latest_report.get('status')} "
            f"review={latest_report.get('human_review_required')}"
        )


def _print_delegation(payload: dict[str, Any]) -> None:
    decisions = payload.get("delegation_policy_decisions") or []
    tasks = payload.get("delegated_tasks") or []
    results = payload.get("delegation_results") or []
    evaluations = payload.get("delegation_evaluations") or []
    team_runs = payload.get("agent_team_runs") or []
    if decisions:
        latest = decisions[-1]
        console.print(
            "[dim]Delegation:[/dim] "
            f"[cyan]{latest.get('decision')}[/cyan] "
            f"roles={', '.join(str(role) for role in latest.get('selected_roles', [])[:4]) or 'none'} "
            f"[dim]{str(latest.get('reason', ''))[:100]}[/dim]"
        )
    if tasks:
        latest_task = tasks[-1]
        console.print(
            "[dim]Delegated task:[/dim] "
            f"[cyan]{latest_task.get('agent_role')}[/cyan] "
            f"{latest_task.get('status')} step={latest_task.get('parent_step_id')} "
            f"risk={latest_task.get('risk_level')}"
        )
    if results:
        latest_result = results[-1]
        console.print(
            "[dim]Delegation result:[/dim] "
            f"[cyan]{latest_result.get('status')}[/cyan] "
            f"review={latest_result.get('requires_human_review')} "
            f"{str(latest_result.get('summary', ''))[:100]}"
        )
    if evaluations:
        latest_eval = evaluations[-1]
        failed = latest_eval.get("failed_checks") or []
        console.print(
            "[dim]Delegation eval:[/dim] "
            f"[cyan]{latest_eval.get('status')}[/cyan] "
            f"failed={str(failed[:2])[:80]}"
        )
    if team_runs:
        latest_team = team_runs[-1]
        console.print(
            "[dim]Agent team:[/dim] "
            f"[cyan]{latest_team.get('status')}[/cyan] "
            f"tasks={len(latest_team.get('delegated_task_ids') or [])} "
            f"limit={latest_team.get('concurrency_limit')}"
        )


def _print_model_routing(payload: dict[str, Any]) -> None:
    routes = payload.get("model_routes") or []
    records = payload.get("model_invocation_records") or []
    fallbacks = payload.get("model_fallback_decisions") or []
    if routes:
        latest = routes[-1]
        console.print(
            "[dim]Model route:[/dim] "
            f"[cyan]{latest.get('task')}[/cyan] "
            f"{latest.get('provider')}/{latest.get('selected_model_id')} "
            f"[dim]risk={latest.get('risk_level')} tools={len(latest.get('tools_bound') or [])}[/dim]"
        )
    if records:
        latest_record = records[-1]
        console.print(
            "[dim]Model call:[/dim] "
            f"[cyan]{latest_record.get('status')}[/cyan] "
            f"{latest_record.get('model_id')} "
            f"[dim]{latest_record.get('duration_ms')}ms cost={latest_record.get('cost_estimate')}[/dim]"
        )
    if fallbacks:
        latest_fallback = fallbacks[-1]
        console.print(
            "[dim]Model fallback:[/dim] "
            f"[cyan]{latest_fallback.get('decision')}[/cyan] "
            f"{latest_fallback.get('from_model_id')} -> {latest_fallback.get('to_model_id') or 'none'}"
        )


def _print_delivery(payload: dict[str, Any]) -> None:
    packages = payload.get("delivery_packages") or []
    manifests = payload.get("artifact_manifests") or []
    contracts = payload.get("delivery_contracts") or []
    if contracts:
        latest_contract = contracts[-1]
        console.print(
            "[dim]Delivery contract:[/dim] "
            f"[cyan]{latest_contract.get('delivery_mode')}[/cyan] "
            f"required={len(latest_contract.get('required_items') or [])} "
            f"status={latest_contract.get('status')}"
        )
    if manifests:
        latest_manifest = manifests[-1]
        console.print(
            "[dim]Delivery manifest:[/dim] "
            f"[cyan]{latest_manifest.get('id')}[/cyan] "
            f"evidence={len(latest_manifest.get('evidence_refs') or [])} "
            f"sql={len(latest_manifest.get('sql_items') or [])} "
            f"missing={', '.join(str(item) for item in (latest_manifest.get('missing_items') or [])[:3]) or 'none'}"
        )
    if packages:
        latest_package = packages[-1]
        report = latest_package.get("user_report_path") or "no-report"
        actions = latest_package.get("next_actions") or []
        console.print(
            "[dim]Delivery package:[/dim] "
            f"[cyan]{latest_package.get('status')}[/cyan] "
            f"{str(latest_package.get('title') or '')[:80]} "
            f"[dim]report={report} next={', '.join(str(item) for item in actions[:3])}[/dim]"
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
