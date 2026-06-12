"""Interactive REPL loop — the main CLI interaction engine.

Features:
  - SSE streaming: receives step-by-step output from LangGraph Server
  - Tool call display: renders tool invocations and results in real-time
  - Approval interrupt: detects interrupt events and triggers approval UI
  - Command handling: /history, /sessions, /plan, /help, /quit
  - Session management: new session, resume session, fork from checkpoint
"""

from __future__ import annotations

import asyncio
import os
import uuid
from typing import Any, Optional

from langgraph_sdk import get_client
from prompt_toolkit import PromptSession as PTKPromptSession
from prompt_toolkit.history import FileHistory
from prompt_toolkit.styles import Style
from rich.console import Console
from rich.markdown import Markdown

from cli.approval import prompt_human_approval
from cli.config import CliRuntimeConfig, build_agent_input_context, build_db_connection_card
from cli.doctor import run_doctor
from cli.events import CliEventAdapter
from cli.display import (
    console,
    print_banner,
    print_clarification,
    print_connection_card,
    print_db_plan,
    print_divider,
    print_doctor_report,
    print_error,
    print_goodbye,
    print_intent,
    print_loop_status,
    print_plan,
    print_plan_review,
    print_cli_event,
    print_session_info,
    print_session_index,
    print_task_card,
    print_tool_call,
    print_tool_result,
    print_warning,
)
from cli.history import SessionHistory
from cli.sessions import SessionIndex, record_from_runtime

# ── Prompt toolkit style ─────────────────────────────────────
PROMPT_STYLE = Style.from_dict({
    "prompt": "bold cyan",
    "separator": "dim",
})

HISTORY_FILE = os.path.expanduser("~/.zuixiaoagent_history")


class AgentRepl:
    """Interactive REPL for the terminal-operating agent."""

    def __init__(
        self,
        server_url: str = "http://localhost:2024",
        api_key: Optional[str] = None,
        thread_id: Optional[str] = None,
        runtime_config: Optional[CliRuntimeConfig] = None,
    ):
        self.runtime_config = runtime_config or CliRuntimeConfig(server_url=server_url, api_key=api_key)
        self.server_url = self.runtime_config.server_url
        self.api_key = self.runtime_config.api_key
        self.thread_id = thread_id
        self._assistant_id: Optional[str] = None

        # SDK client (not async context — we use it directly)
        self.client = get_client(url=self.server_url, api_key=self.api_key)
        self.history_mgr = SessionHistory(self.client)
        self.session_index = SessionIndex()
        self.event_adapter = CliEventAdapter(thread_id or "unknown")
        self.running = False

        self.pt_session = PTKPromptSession(
            history=FileHistory(HISTORY_FILE),
            style=PROMPT_STYLE,
        )

    # ── Main loop ────────────────────────────────────────

    async def run(self):
        """Start the interactive REPL."""
        self.running = True

        # Ensure assistant is resolved and thread exists
        await self._ensure_assistant()
        await self._ensure_thread()

        print_banner()
        print_session_info(
            self.thread_id or "new",
            self._assistant_id or "agent",
            0,
        )
        self.event_adapter.thread_id = self.thread_id or "unknown"
        print_connection_card(build_db_connection_card(self.runtime_config))

        while self.running:
            try:
                user_input = await self._get_input()
                if user_input is None:
                    continue
                if not user_input.strip():
                    continue
                if await self._handle_command(user_input):
                    continue
                await self._send_message(user_input)
            except KeyboardInterrupt:
                console.print("\n[dim]Current run interrupted locally. Press Ctrl+C again to quit.[/dim]")
                try:
                    await self._get_input()
                except KeyboardInterrupt:
                    break
            except EOFError:
                break

        print_goodbye()

    # ── Initialization ──────────────────────────────────

    async def _ensure_assistant(self):
        """Find or reuse the agent assistant."""
        try:
            assists = await self.client.assistants.search()
            if assists:
                self._assistant_id = assists[0].get("assistant_id", assists[0].get("name", "agent"))
            else:
                self._assistant_id = "agent"
        except Exception:
            self._assistant_id = "agent"

    async def _ensure_thread(self):
        """Create thread if new, or verify existing."""
        if self.thread_id:
            try:
                await self.client.threads.get(self.thread_id)
            except Exception:
                # Thread doesn't exist on server — create it
                thread = await self.client.threads.create(thread_id=self.thread_id)
                self.thread_id = thread["thread_id"]
        else:
            self.thread_id = str(uuid.uuid4())
            thread = await self.client.threads.create(thread_id=self.thread_id)
            self.thread_id = thread["thread_id"]

    # ── Input ────────────────────────────────────────────

    async def _get_input(self) -> Optional[str]:
        try:
            return await self.pt_session.prompt_async(
                [("class:prompt", "> "), ("class:separator", "")],
            )
        except (EOFError, KeyboardInterrupt):
            raise

    # ── Command handling ─────────────────────────────────

    async def _handle_command(self, text: str) -> bool:
        if not text.startswith("/"):
            return False
        parts = text.strip().split()
        cmd = parts[0].lower()
        args = parts[1:]

        handlers = {
            "/quit": lambda: setattr(self, "running", False),
            "/exit": lambda: setattr(self, "running", False),
            "/help": self._show_help,
            "/sessions": lambda: None,  # handled async below
            "/history": lambda: None,
            "/info": lambda: None,
            "/new": lambda: None,
            "/resume": lambda: None,
            "/plan": lambda: None,
            "/clear": lambda: console.clear() or print_banner(),
        }

        if cmd == "/quit" or cmd == "/exit":
            self.running = False
            return True
        elif cmd == "/help":
            self._show_help()
            return True
        elif cmd == "/sessions":
            await self._cmd_sessions()
            return True
        elif cmd == "/history":
            await self._cmd_history(args[0] if args else None)
            return True
        elif cmd == "/info":
            await self._cmd_info()
            return True
        elif cmd == "/new":
            self.thread_id = str(uuid.uuid4())
            await self._ensure_thread()
            self._save_session_index()
            console.print(f"[green]New session: {self.thread_id[:12]}...[/green]")
            return True
        elif cmd == "/resume":
            if args:
                self.thread_id = args[0]
                await self._ensure_thread()
                self._save_session_index()
                console.print(f"[green]Resumed: {self.thread_id[:12]}...[/green]")
            else:
                console.print("[red]Usage: /resume <thread_id>[/red]")
            return True
        elif cmd == "/plan":
            await self._cmd_show_plan()
            return True
        elif cmd == "/risk":
            await self._cmd_risk()
            return True
        elif cmd == "/approvals":
            await self._cmd_approvals()
            return True
        elif cmd == "/artifacts":
            await self._cmd_artifacts()
            return True
        elif cmd == "/db":
            print_connection_card(build_db_connection_card(self.runtime_config))
            return True
        elif cmd == "/schema":
            await self._send_message(_schema_prompt(args))
            return True
        elif cmd == "/tables":
            await self._send_message("列出当前 PostgreSQL 数据库中的用户表，按 schema 分组，只执行只读元数据查询。")
            return True
        elif cmd == "/readonly":
            if args and args[0].lower() in {"on", "true", "1"}:
                self.runtime_config = _replace_config(self.runtime_config, readonly=True)
                console.print("[green]Readonly mode enabled for new runs.[/green]")
            elif args and args[0].lower() in {"off", "false", "0"}:
                self.runtime_config = _replace_config(self.runtime_config, readonly=False)
                console.print("[yellow]Readonly mode disabled for new runs; backend safety still applies.[/yellow]")
            else:
                console.print(f"[dim]Readonly:[/dim] {self.runtime_config.readonly}")
            return True
        elif cmd == "/doctor":
            report = await run_doctor(self.runtime_config)
            print_doctor_report(report)
            return True
        elif cmd == "/cancel":
            await self._cmd_cancel()
            return True
        elif cmd == "/archive":
            target = args[0] if args else self.thread_id
            if target and self.session_index.archive(target):
                console.print(f"[green]Archived: {target[:12]}...[/green]")
            else:
                console.print("[yellow]No indexed session matched.[/yellow]")
            return True
        elif cmd == "/clear":
            console.clear()
            print_banner()
            return True
        else:
            console.print(f"[red]Unknown: {cmd}[/red] — /help for commands")
            return True

    # ── Agent interaction ────────────────────────────────

    async def _send_message(self, content: str):
        console.print()
        print_divider()

        try:
            stream_input = {
                "messages": [{"role": "user", "content": content}],
                **build_agent_input_context(self.runtime_config),
            }
            async for event in self.client.runs.stream(
                thread_id=self.thread_id,
                assistant_id=self._assistant_id,
                input=stream_input,
                stream_mode="updates",
            ):
                self._process_event(event)
        except Exception as e:
            print_error(f"Agent error: {e}")

        await self._save_session_index_async()
        print_divider()
        console.print()

    def _process_event(self, event: Any):
        """Process a single streaming event from the graph."""
        event_type = getattr(event, "event", str(event))
        data = getattr(event, "data", event)

        if event_type == "metadata":
            return

        if not isinstance(data, dict):
            return

        for cli_event in self.event_adapter.events_from_stream_data(data):
            print_cli_event(cli_event)

        for node_name, output in data.items():
            if node_name in ("__start__", "__interrupt__"):
                continue

            if node_name == "task_planner":
                db_plan = output.get("db_task_plan") if isinstance(output, dict) else None
                plan = output.get("plan") if isinstance(output, dict) else None
                if db_plan:
                    print_db_plan(db_plan)
                    print_plan_review(output.get("plan_review") or {})
                elif plan:
                    print_plan(plan)

            elif node_name in ("state_recovery", "step_scheduler", "normalize_observation", "verify_step", "tool_policy_gate", "error_handler"):
                if isinstance(output, dict):
                    print_loop_status(node_name, output)
                    if node_name == "error_handler":
                        self._process_messages(output.get("messages", []))

            elif node_name == "intent_validator":
                intent = output.get("current_intent") if isinstance(output, dict) else None
                if intent:
                    print_intent(intent)
                    print_task_card(output.get("task_card") or {})

            elif node_name == "clarification_gate":
                request = output.get("pending_clarification") if isinstance(output, dict) else None
                if request:
                    print_clarification(request)
                self._process_messages(output.get("messages", []))

            elif node_name == "llm_reason":
                if isinstance(output, dict):
                    print_loop_status(node_name, output)
                self._process_messages(output.get("messages", []))

            elif node_name == "execute_tools":
                if isinstance(output, dict):
                    print_loop_status(node_name, output)
                self._process_messages(output.get("messages", []), is_tool=True)

            elif node_name == "final_report":
                if isinstance(output, dict):
                    print_loop_status(node_name, output)
                    self._process_messages(output.get("messages", []))

            elif node_name == "memory_compactor" and output is not None:
                console.print("[dim]📦 Memory compacted[/dim]")

    def _process_messages(self, msgs: list, is_tool: bool = False):
        """Process and display messages — handles both dict and object formats."""
        for msg in (msgs or []):
            # API serializes messages as plain dicts; local invocation uses objects
            content = msg.get("content", "") if isinstance(msg, dict) else getattr(msg, "content", "")
            tool_calls = msg.get("tool_calls") if isinstance(msg, dict) else getattr(msg, "tool_calls", None)
            name = msg.get("name", "") if isinstance(msg, dict) else getattr(msg, "name", "")
            msg_type = msg.get("type", "") if isinstance(msg, dict) else getattr(msg, "type", "")

            if is_tool and name:
                print_tool_result(str(name), str(content)[:300])

            elif tool_calls:
                for tc in tool_calls:
                    tc_name = tc.get("name", "?") if isinstance(tc, dict) else getattr(tc, "name", "?")
                    tc_args = tc.get("args", {}) if isinstance(tc, dict) else getattr(tc, "args", {})
                    print_tool_call(tc_name, tc_args)

            elif content and msg_type != "system":
                console.print(Markdown(str(content)))

    # ── Slash commands ───────────────────────────────────

    def _show_help(self):
        console.print(Markdown("""
[bold]Commands:[/bold]
  [cyan]/help[/cyan]       Show help
  [cyan]/quit[/cyan]       Exit
  [cyan]/new[/cyan]        New session
  [cyan]/resume[/cyan] ID  Resume session
  [cyan]/sessions[/cyan]   List sessions
  [cyan]/history[/cyan]    Show checkpoint history
  [cyan]/info[/cyan]       Agent info
  [cyan]/plan[/cyan]       Current plan
  [cyan]/risk[/cyan]       Current risk and safety state
  [cyan]/approvals[/cyan]  Pending and recent SQL approvals
  [cyan]/artifacts[/cyan]  Delivery reports and artifact paths
  [cyan]/db[/cyan]         Current PostgreSQL target card
  [cyan]/schema[/cyan]     Inspect schema/table metadata
  [cyan]/tables[/cyan]     List user tables
  [cyan]/doctor[/cyan]     Diagnose server, database, workspace
  [cyan]/readonly[/cyan]   Show or set readonly mode
  [cyan]/archive[/cyan]    Archive indexed session
  [cyan]/clear[/cyan]      Clear screen
"""))

    async def _cmd_sessions(self):
        try:
            threads = await self.client.threads.search(limit=20)
            for t in threads:
                tid = t.get("thread_id", "?")
                marker = "→ " if tid == self.thread_id else "  "
                console.print(f"{marker}[cyan]{tid[:12]}...[/cyan]")
            print_session_index(self.session_index.list(limit=20), current_thread_id=self.thread_id)
        except Exception as e:
            console.print(f"[red]{e}[/red]")

    async def _cmd_history(self, tid: Optional[str] = None):
        await self.history_mgr.show_history(tid or self.thread_id)

    async def _cmd_info(self):
        try:
            import httpx
            async with httpx.AsyncClient() as hc:
                headers = {"X-API-Key": self.api_key} if self.api_key else {}
                resp = await hc.get(f"{self.server_url}/agent/info", headers=headers)
                console.print_json(data=resp.json())
        except Exception as e:
            console.print(f"[dim]{e}[/dim]")

    async def _cmd_show_plan(self):
        try:
            state = await self.client.threads.get_state(self.thread_id)
            if state and state.get("values"):
                vals = state["values"]
                plan = vals.get("plan")
                tasks = vals.get("task_stack", [])
                idx = vals.get("current_task_index", 0)
                if plan:
                    console.print(Markdown(plan))
                if tasks:
                    for i, t in enumerate(tasks):
                        icon = {"pending": "⬜", "running": "🔄", "completed": "✅", "failed": "❌"}.get(t.get("status", ""), "❓")
                        marker = "→ " if i == idx else "  "
                        console.print(f"{marker}{icon} [{t.get('id','?')}] {t.get('description','')}")
                if not plan and not tasks:
                    console.print("[dim]No active plan.[/dim]")
        except Exception as e:
            console.print(f"[dim]{e}[/dim]")

    async def _cmd_risk(self):
        vals = await self._current_values()
        intent = vals.get("current_intent") or {}
        runtime = vals.get("db_task_runtime") or {}
        safety = vals.get("security_policy_decisions") or []
        sql_reports = vals.get("sql_safety_reports") or []
        console.print_json(
            data={
                "intent_risk": intent.get("risk_level"),
                "runtime_risk": runtime.get("risk_level"),
                "latest_security_decision": safety[-1] if safety else None,
                "latest_sql_safety_report": sql_reports[-1] if sql_reports else None,
            }
        )

    async def _cmd_approvals(self):
        vals = await self._current_values()
        pending = vals.get("pending_approval")
        card = vals.get("approval_card")
        decisions = vals.get("approval_decisions") or []
        console.print_json(data={"pending_approval": pending, "approval_card": card, "approval_decisions": decisions[-10:]})

    async def _cmd_artifacts(self):
        vals = await self._current_values()
        console.print_json(
            data={
                "delivery_packages": vals.get("delivery_packages") or [],
                "artifact_manifests": vals.get("artifact_manifests") or [],
                "artifact_records": vals.get("artifact_records") or [],
            }
        )

    async def _cmd_cancel(self):
        if not self.thread_id:
            console.print("[yellow]No active session.[/yellow]")
            return
        try:
            runs = await self.client.runs.list(self.thread_id, limit=10)
            candidates = [
                run for run in runs
                if str(run.get("status") or "").lower() in {"pending", "running"}
            ]
            if not candidates:
                console.print("[dim]No pending or running run to cancel.[/dim]")
                return
            run_id = candidates[0].get("run_id") or candidates[0].get("id")
            await self.client.runs.cancel(self.thread_id, run_id, wait=False, action="interrupt")
            console.print(f"[yellow]Cancelled run: {str(run_id)[:12]}...[/yellow]")
        except Exception as e:
            console.print(f"[red]Cancel failed: {e}[/red]")

    async def _current_values(self) -> dict[str, Any]:
        try:
            state = await self.client.threads.get_state(self.thread_id)
            return state.get("values", {}) if state else {}
        except Exception as e:
            console.print(f"[dim]{e}[/dim]")
            return {}

    def _save_session_index(self):
        if not self.runtime_config.save_session or not self.thread_id:
            return
        try:
            vals: dict[str, Any] = {}
            # Avoid blocking on async state fetch here; the next command/run will refresh
            # via explicit state reads. This record still indexes the thread safely.
            self.session_index.upsert(
                record_from_runtime(
                    self.runtime_config,
                    thread_id=self.thread_id,
                    state_values=vals,
                )
            )
        except Exception:
            pass

    async def _save_session_index_async(self):
        if not self.runtime_config.save_session or not self.thread_id:
            return
        vals = await self._current_values()
        try:
            self.session_index.upsert(
                record_from_runtime(
                    self.runtime_config,
                    thread_id=self.thread_id,
                    state_values=vals,
                )
            )
        except Exception:
            pass


def _schema_prompt(args: list[str]) -> str:
    if args:
        target = " ".join(args)
        return f"查看 PostgreSQL 对象 {target} 的结构摘要，只执行只读元数据查询。"
    return "查看当前 PostgreSQL 数据库的 schema 摘要，只执行只读元数据查询。"


def _replace_config(config: CliRuntimeConfig, **updates: Any) -> CliRuntimeConfig:
    data = dict(config.__dict__)
    data.update(updates)
    return CliRuntimeConfig(**data)
