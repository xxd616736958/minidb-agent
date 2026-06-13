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
from pathlib import Path
from typing import Any, Optional

from langgraph_sdk import get_client
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit import PromptSession as PTKPromptSession
from prompt_toolkit.history import FileHistory
from prompt_toolkit.shortcuts import CompleteStyle
from prompt_toolkit.styles import Style
from rich.console import Console
from rich.markdown import Markdown

from cli.approval import prompt_sql_approval
from cli.config import CliRuntimeConfig, build_agent_input_context, build_db_connection_card, persist_runtime_defaults
from cli.doctor import run_doctor
from cli.events import CliEventAdapter
from cli.local_server import ensure_local_server
from cli.setup_flow import prompt_reconnect_config
from cli.display import (
    console,
    print_banner,
    print_clarification,
    print_connection_card,
    print_connection_summary,
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
    print_agent_status,
    print_session_info,
    print_session_index,
    print_task_card,
    print_tool_call,
    print_tool_done,
    print_tool_start,
    print_tool_result,
    print_warning,
)
from cli.history import SessionHistory
from cli.sessions import SessionIndex, has_resume_content, record_from_runtime
from cli.server_info import fetch_agent_info
from cli.session_picker import choose_session

# ── Prompt toolkit style ─────────────────────────────────────
PROMPT_STYLE = Style.from_dict({
    "prompt": "bold cyan",
    "separator": "dim",
})

HISTORY_FILE = os.path.expanduser("~/.minidb_agent_history")


SLASH_COMMANDS: dict[str, str] = {
    "/help": "Show commands",
    "/quit": "Exit",
    "/exit": "Exit",
    "/new": "Start a new session",
    "/resume": "Resume a session",
    "/sessions": "List sessions",
    "/history": "Show checkpoint history",
    "/info": "Show agent info",
    "/plan": "Show current plan",
    "/risk": "Show current risk state",
    "/approvals": "Show SQL approvals",
    "/artifacts": "Show delivery artifacts",
    "/db": "Show PostgreSQL target",
    "/reconnectdb": "Reconnect PostgreSQL target",
    "/schema": "Inspect schema/table",
    "/tables": "List user tables",
    "/readonly": "Show or set readonly mode",
    "/doctor": "Run diagnostics",
    "/cancel": "Cancel running task",
    "/archive": "Archive current session",
    "/clear": "Clear screen",
}


class SlashCommandCompleter(Completer):
    """Complete slash commands when the prompt starts with '/'."""

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        if not text.startswith("/") or " " in text:
            return
        for command, description in SLASH_COMMANDS.items():
            if command.startswith(text):
                yield Completion(
                    command,
                    start_position=-len(text),
                    display=command,
                    display_meta=description,
                )


class AgentRepl:
    """Interactive REPL for MiniDB Agent."""

    def __init__(
        self,
        server_url: str = "http://127.0.0.1:2024",
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
        self._last_approval_card: dict[str, Any] | None = None
        self._server_info: dict[str, Any] | None = None
        self._active_tool_calls: dict[str, dict[str, Any]] = {}
        self._printed_tool_call_ids: set[str] = set()
        self._last_tool_summary: str | None = None
        self._recent_tool_summaries: list[str] = []

        self.pt_session = PTKPromptSession(
            history=FileHistory(HISTORY_FILE),
            style=PROMPT_STYLE,
            completer=SlashCommandCompleter(),
            complete_while_typing=True,
            complete_style=CompleteStyle.MULTI_COLUMN,
        )

    # ── Main loop ────────────────────────────────────────

    async def run(self):
        """Start the interactive REPL."""
        self.running = True

        # Ensure assistant is resolved and thread exists
        await self._ensure_assistant()
        await self._ensure_thread()
        self._server_info = await self._safe_agent_info()

        print_banner()
        model_name, tools_count = await self._session_display_info(self._server_info)
        print_session_info(
            self.thread_id or "new",
            model_name,
            tools_count,
        )
        self.event_adapter.thread_id = self.thread_id or "unknown"
        print_connection_summary(build_db_connection_card(self.runtime_config, self._server_info))
        console.print("[dim]Type / for commands. Use /db for connection details.[/dim]")
        console.print()

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

    async def _session_display_info(self, info: dict[str, Any] | None = None) -> tuple[str, int]:
        model_name = os.environ.get("LLM_MODEL") or "deepseek-chat"
        tools_count = 0
        if isinstance(info, dict):
            model_name = str(info.get("model") or model_name)
            tools = info.get("tools") or []
            tools_count = len(tools)
        if tools_count == 0 and self.runtime_config.verbose:
            try:
                from tools.registry import registry

                if registry.count == 0:
                    registry.discover("tools.builtin")
                tools_count = registry.count
            except Exception:
                tools_count = 0
        return model_name, tools_count

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
            thread = await self.client.threads.create()
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
            console.print(f"[green]New session: {self.thread_id[:12]}...[/green]")
            return True
        elif cmd == "/resume":
            if args:
                self.thread_id = args[0]
                await self._ensure_thread()
                console.print(f"[green]Resumed: {self.thread_id[:12]}...[/green]")
            else:
                selected = await self._select_session_to_resume()
                if selected:
                    self.thread_id = selected
                    await self._ensure_thread()
                    self.event_adapter.thread_id = self.thread_id
                    console.print(f"[green]Resumed: {self.thread_id[:12]}...[/green]")
                else:
                    console.print("[dim]No session selected.[/dim]")
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
            self._server_info = await self._safe_agent_info()
            print_connection_card(build_db_connection_card(self.runtime_config, self._server_info))
            return True
        elif cmd == "/reconnectdb":
            await self._cmd_reconnectdb()
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
        if self.runtime_config.verbose:
            console.print()
            print_divider()

        try:
            stream_input = {
                "messages": [{"role": "user", "content": content}],
                **build_agent_input_context(self.runtime_config, self._server_info),
            }
            await self._stream_run(stream_input)
            await self._finalize_stream_status()
            await self._handle_pending_approval()
        except Exception as e:
            print_error(f"Agent error: {e}")

        await self._save_session_index_async()
        if self.runtime_config.verbose:
            print_divider()
        console.print()

    async def _stream_run(self, stream_input: dict[str, Any]) -> None:
        async for event in self.client.runs.stream(
            thread_id=self.thread_id,
            assistant_id=self._assistant_id,
            input=stream_input,
            stream_mode="updates",
            config={"recursion_limit": self.runtime_config.recursion_limit},
        ):
            self._process_event(event)

    async def _handle_pending_approval(self) -> None:
        card = self._last_approval_card
        if not card:
            return
        self._last_approval_card = None
        decision = prompt_sql_approval(card)
        if not decision:
            return
        stream_input = {
            "messages": [
                {
                    "role": "user",
                    "content": f"Approval response: {decision.get('action')} {decision.get('approval_id')}",
                }
            ],
            "cli_approval_response": decision,
            **build_agent_input_context(self.runtime_config, self._server_info),
        }
        await self._stream_run(stream_input)
        await self._finalize_stream_status()

    def _process_event(self, event: Any):
        """Process a single streaming event from the graph."""
        event_type = getattr(event, "event", str(event))
        data = getattr(event, "data", event)

        if event_type == "metadata":
            return

        if not isinstance(data, dict):
            return

        for cli_event in self.event_adapter.events_from_stream_data(data):
            print_cli_event(cli_event, verbose=self.runtime_config.verbose)

        for node_name, output in data.items():
            if node_name in ("__start__", "__interrupt__"):
                continue

            if node_name == "task_planner":
                if not self.runtime_config.verbose:
                    continue
                db_plan = output.get("db_task_plan") if isinstance(output, dict) else None
                plan = output.get("plan") if isinstance(output, dict) else None
                if db_plan:
                    print_db_plan(db_plan)
                    print_plan_review(output.get("plan_review") or {})
                elif plan:
                    print_plan(plan)

            elif node_name in ("state_recovery", "step_scheduler", "normalize_observation", "verify_step", "tool_policy_gate", "error_handler"):
                if isinstance(output, dict):
                    if self.runtime_config.verbose:
                        print_loop_status(node_name, output)
                    elif node_name in {"step_scheduler", "verify_step"}:
                        self._print_compact_runtime(node_name, output)
                    if node_name == "tool_policy_gate" and output.get("approval_card"):
                        self._last_approval_card = output.get("approval_card")
                        if not self.runtime_config.verbose:
                            console.print("[red]Approval required[/red] [dim]review the SQL action before execution[/dim]")
                    if node_name == "error_handler" and self.runtime_config.verbose:
                        self._process_messages(output.get("messages", []))

            elif node_name == "intent_validator":
                if not self.runtime_config.verbose:
                    continue
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
                    if self.runtime_config.verbose:
                        print_loop_status(node_name, output)
                self._process_messages(output.get("messages", []), show_tool_json=False)

            elif node_name == "execute_tools":
                if isinstance(output, dict):
                    if self.runtime_config.verbose:
                        print_loop_status(node_name, output)
                    if not self.runtime_config.verbose:
                        self._process_tool_results(output.get("tool_execution_results") or [])
                if self.runtime_config.verbose:
                    self._process_messages(output.get("messages", []), is_tool=True)

            elif node_name == "final_report":
                if isinstance(output, dict):
                    if self.runtime_config.verbose:
                        print_loop_status(node_name, output)
                    elif not self.runtime_config.verbose:
                        self._print_delivery_status(output)

            elif node_name == "memory_compactor" and output is not None and self.runtime_config.verbose:
                console.print("[dim]📦 Memory compacted[/dim]")

    async def _finalize_stream_status(self) -> None:
        vals = await self._current_values()
        run_error = await self._latest_run_error()
        if run_error:
            self._finish_dangling_tools()
            print_error(self._run_error_message(run_error))
            return
        if not vals:
            self._finish_dangling_tools()
            return
        pending = vals.get("pending_approval")
        if pending and pending.get("status") == "pending":
            if not self._last_approval_card:
                self._last_approval_card = vals.get("approval_card")
            print_agent_status("waiting for approval")
            return
        self._finish_dangling_tools()
        violation = vals.get("policy_violation")
        if violation:
            print_error(f"Blocked: {violation.get('message') or 'tool policy violation'}")
            return
        error = vals.get("error")
        if error:
            print_error(str(error))
            return
        runtime = vals.get("db_task_runtime") or {}
        task_status = str(runtime.get("task_status") or vals.get("loop_status") or "")
        if task_status in {"blocked", "failed", "error"}:
            print_error(f"Turn {task_status}: {runtime.get('current_step_id') or 'unknown step'}")

    async def _latest_run_error(self) -> dict[str, Any] | None:
        if not self.thread_id:
            return None
        try:
            runs = await self.client.runs.list(self.thread_id, limit=1)
        except Exception:
            return None
        if not runs:
            return None
        latest = runs[0]
        if str(latest.get("status") or "").lower() != "error":
            return None
        run_id = str(latest.get("run_id") or latest.get("id") or "")
        detail: Any = None
        if run_id:
            try:
                detail = await self.client.runs.join(self.thread_id, run_id)
            except Exception:
                detail = None
        if isinstance(detail, dict) and detail.get("__error__"):
            return {"run_id": run_id, **detail["__error__"]}
        return {
            "run_id": run_id,
            "error": str(latest.get("status") or "error"),
            "message": "Agent run failed before reaching a terminal state.",
        }

    def _run_error_message(self, error: dict[str, Any]) -> str:
        kind = str(error.get("error") or "AgentRunError")
        message = str(error.get("message") or "Agent run failed.")
        return f"{kind}: {message}"

    def _finish_dangling_tools(self) -> None:
        if not self._active_tool_calls:
            return
        pending = list(self._active_tool_calls.values())
        self._active_tool_calls.clear()
        for item in pending:
            label = str(item.get("label") or item.get("name") or "tool")
            print_tool_done(label, "stream ended before a tool result was returned", success=False)

    def _print_delivery_status(self, output: dict[str, Any]) -> None:
        sections = output.get("report_sections") or []
        if sections:
            self._print_report_sections(sections)
        packages = output.get("delivery_packages") or []
        if packages:
            latest = packages[-1]
            status = latest.get("status") or "ready"
            title = latest.get("title") or "delivery"
            print_agent_status(f"done: {title} ({status})")

    def _print_report_sections(self, sections: list[dict[str, Any]]) -> None:
        preferred = ("assistant_report", "findings", "diagnosis", "execution", "risk", "verification", "next_steps")
        printed = False
        for purpose in preferred:
            for section in sections:
                if section.get("purpose") != purpose:
                    continue
                content = str(section.get("content") or "").strip()
                if not content or content == "No content.":
                    continue
                title = str(section.get("title") or purpose)
                console.print(Markdown(f"### {title}\n\n{content}"))
                printed = True
                break
        if printed:
            return

    def _process_messages(self, msgs: list, is_tool: bool = False, show_tool_json: bool = True):
        """Process and display messages — handles both dict and object formats."""
        for msg in (msgs or []):
            # API serializes messages as plain dicts; local invocation uses objects
            content = msg.get("content", "") if isinstance(msg, dict) else getattr(msg, "content", "")
            tool_calls = msg.get("tool_calls") if isinstance(msg, dict) else getattr(msg, "tool_calls", None)
            name = msg.get("name", "") if isinstance(msg, dict) else getattr(msg, "name", "")
            msg_type = msg.get("type", "") if isinstance(msg, dict) else getattr(msg, "type", "")

            if is_tool and name:
                if not self.runtime_config.verbose:
                    summary, success = self._tool_result_summary(content)
                    print_tool_done(str(name), summary, success=success)
                    continue
                print_tool_result(str(name), str(content)[:300])

            elif tool_calls:
                for tc in tool_calls:
                    tc_name = tc.get("name", "?") if isinstance(tc, dict) else getattr(tc, "name", "?")
                    tc_args = tc.get("args", {}) if isinstance(tc, dict) else getattr(tc, "args", {})
                    tc_id = str(tc.get("id", "") if isinstance(tc, dict) else getattr(tc, "id", ""))
                    args_dict = tc_args if isinstance(tc_args, dict) else {}
                    label = self._tool_display_name(str(tc_name), args_dict)
                    if self.runtime_config.verbose:
                        print_tool_call(tc_name, tc_args)
                    elif tc_id not in self._printed_tool_call_ids:
                        self._printed_tool_call_ids.add(tc_id)
                        if tc_id:
                            self._active_tool_calls[tc_id] = {
                                "name": str(tc_name),
                                "args": args_dict,
                                "label": label,
                            }
                        print_tool_start(label)

            elif content and msg_type != "system":
                if not self.runtime_config.verbose and not show_tool_json and (
                    self._looks_like_tool_result(content)
                    or self._looks_like_tool_markup(content)
                    or self._looks_like_tool_echo(content)
                ):
                    continue
                console.print(Markdown(str(content)))

    def _print_compact_runtime(self, node_name: str, output: dict[str, Any]) -> None:
        runtime = output.get("db_task_runtime") or {}
        if not runtime:
            return
        if node_name == "step_scheduler":
            phase = runtime.get("current_phase")
            step = runtime.get("current_step_id")
            if phase and step:
                print_agent_status(f"{phase}: {step}")
        elif node_name == "verify_step":
            results = output.get("verification_results") or []
            if results:
                latest = results[-1]
                print_agent_status(f"verify: {latest.get('step_id')} {latest.get('status')}")

    def _process_tool_results(self, results: list[dict[str, Any]]) -> None:
        for result in results or []:
            tool_name = str(result.get("tool_name") or "tool")
            call_id = str(result.get("tool_call_id") or "")
            started = self._active_tool_calls.pop(call_id, {}) if call_id else {}
            label = str(started.get("label") or self._tool_display_name(tool_name, result=result))
            preview = self._tool_result_preview(result)
            print_tool_done(label, preview, success=bool(result.get("success", True)))
            self._last_tool_summary = str(result.get("summary") or "")
            if self._last_tool_summary:
                self._recent_tool_summaries.append(self._last_tool_summary)
                self._recent_tool_summaries = self._recent_tool_summaries[-20:]

    def _looks_like_tool_result(self, content: Any) -> bool:
        text = str(content)
        return text.strip().startswith("{") and "mini_agent_postgres_tool_result" in text

    def _looks_like_tool_markup(self, content: Any) -> bool:
        text = str(content)
        return "tool_calls" in text and "invoke name=" in text

    def _looks_like_tool_echo(self, content: Any) -> bool:
        text = str(content).strip()
        if not text:
            return False
        return bool(self._last_tool_summary and text == self._last_tool_summary) or text in self._recent_tool_summaries

    def _tool_display_name(self, tool_name: str, args: dict[str, Any] | None = None, result: dict[str, Any] | None = None) -> str:
        args = args or {}
        result = result or {}
        if tool_name == "postgres_query_readonly":
            sql = str(args.get("sql") or result.get("payload", {}).get("sql") or "read-only SQL")
            return self._compact_sql(sql)
        if tool_name == "postgres_top_queries":
            sort_by = args.get("sort_by") or result.get("payload", {}).get("sort_by") or "resources"
            limit = args.get("limit") or result.get("row_count") or ""
            suffix = f" limit {limit}" if limit else ""
            return f"postgres top queries by {sort_by}{suffix}"
        if tool_name == "postgres_list_objects":
            schema = args.get("schema_name") or args.get("schema") or result.get("payload", {}).get("schema") or "public"
            object_type = args.get("object_type") or result.get("payload", {}).get("object_type") or "table"
            return f"list {schema}.{object_type}s"
        if tool_name == "postgres_schema_overview":
            return "inspect PostgreSQL target overview"
        if tool_name == "postgres_object_detail":
            schema = args.get("schema_name") or args.get("schema") or "public"
            name = args.get("object_name") or args.get("table") or args.get("name") or "object"
            return f"describe {schema}.{name}"
        if tool_name == "postgres_lock_inspect":
            return "inspect locks/activity"
        if tool_name == "postgres_connection_check":
            return "check PostgreSQL connection"
        if tool_name == "postgres_list_databases":
            return "list PostgreSQL databases"
        return tool_name

    def _tool_result_preview(self, result: dict[str, Any]) -> str:
        result_type = str(result.get("result_type") or "")
        payload = result.get("payload") or {}
        summary = str(result.get("summary") or result_type or "completed")
        if result_type == "top_queries":
            rows = payload.get("queries") or payload.get("active_queries") or []
            if rows:
                first = rows[0]
                query = str(first.get("query_preview") or first.get("query") or "").strip()
                metric = first.get("total_exec_time") or first.get("mean_exec_time") or first.get("calls")
                metric_text = f" metric={metric}" if metric is not None else ""
                return f"{summary}; top: {self._compact_sql(query)}{metric_text}"
        if result_type == "schema_summary":
            objects = payload.get("objects") or payload.get("schemas") or []
            if payload.get("connection") and payload.get("tables") is not None:
                conn = payload.get("connection") or {}
                schemas = payload.get("schemas") or []
                tables = payload.get("tables") or []
                db = conn.get("database") or "unknown"
                user = conn.get("user") or "unknown"
                return f"{summary}; database={db} user={user} schemas={len(schemas)} tables={len(tables)}"
            if objects:
                names = []
                for item in objects[:5]:
                    names.append(str(item.get("name") or item.get("schema_name") or item.get("schema") or item))
                more = f", +{len(objects) - 5} more" if len(objects) > 5 else ""
                return f"{summary}; {', '.join(names)}{more}"
        if result_type == "database_list":
            databases = payload.get("databases") or []
            if databases:
                names = [str(item.get("database") or item) for item in databases[:8]]
                more = f", +{len(databases) - 8} more" if len(databases) > 8 else ""
                return f"{summary}; {', '.join(names)}{more}"
        if result_type == "query_result":
            rows = payload.get("rows") or []
            if rows:
                return f"{summary}; {self._row_preview(rows[0])}"
        if result_type == "object_detail":
            basic = payload.get("basic") or {}
            columns = payload.get("columns") or []
            name = ".".join(str(part) for part in [basic.get("schema"), basic.get("name")] if part)
            return f"{summary}; {name} columns={len(columns)}"
        if result_type == "connection_status":
            database = payload.get("database")
            user = payload.get("user")
            return f"{summary}; database={database} user={user}"
        return summary

    def _compact_sql(self, sql: str, limit: int = 120) -> str:
        text = " ".join(str(sql).split())
        if len(text) > limit:
            return text[: limit - 1] + "…"
        return text

    def _row_preview(self, row: Any, limit: int = 160) -> str:
        if not isinstance(row, dict):
            return self._compact_sql(str(row), limit=limit)
        parts = [f"{key}={value}" for key, value in list(row.items())[:6]]
        return self._compact_sql(", ".join(parts), limit=limit)

    def _tool_result_summary(self, content: Any) -> tuple[str | None, bool]:
        try:
            import json

            parsed = json.loads(str(content))
            result = parsed.get("result") if isinstance(parsed, dict) else None
            if not isinstance(result, dict):
                return None, True
            return str(result.get("summary") or result.get("result_type") or ""), bool(result.get("success", True))
        except Exception:
            return None, True

    # ── Slash commands ───────────────────────────────────

    def _show_help(self):
        for command, description in SLASH_COMMANDS.items():
            console.print(f"[cyan]{command:<12}[/cyan] [dim]{description}[/dim]")

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

    async def _select_session_to_resume(self) -> str | None:
        project_dir = str(Path(self.runtime_config.workspace).expanduser().resolve())
        project_records = self.session_index.list(
            project_dir=project_dir,
            limit=30,
        )
        records = project_records or self.session_index.list(limit=30)
        if self.thread_id:
            records = [record for record in records if record.get("thread_id") != self.thread_id]
        records = [record for record in records if has_resume_content(record)]
        return await choose_session(records)

    async def _cmd_history(self, tid: Optional[str] = None):
        await self.history_mgr.show_history(tid or self.thread_id)

    async def _cmd_info(self):
        try:
            console.print_json(data=await self._agent_info())
        except Exception as e:
            console.print(f"[dim]{e}[/dim]")

    async def _agent_info(self) -> dict[str, Any]:
        info = await fetch_agent_info(self.runtime_config)
        if info is None:
            raise RuntimeError("agent info unavailable")
        return info

    async def _safe_agent_info(self) -> dict[str, Any] | None:
        return await fetch_agent_info(self.runtime_config)

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

    async def _cmd_reconnectdb(self):
        new_config = prompt_reconnect_config(self.runtime_config)
        try:
            self.runtime_config = await ensure_local_server(new_config, force_restart=True)
            persist_runtime_defaults(self.runtime_config)
        except Exception as e:
            console.print(f"[red]Reconnect failed: {e}[/red]")
            return
        self.server_url = self.runtime_config.server_url
        self.api_key = self.runtime_config.api_key
        self.client = get_client(url=self.server_url, api_key=self.api_key)
        self.history_mgr = SessionHistory(self.client)
        await self._ensure_assistant()
        self.thread_id = None
        await self._ensure_thread()
        self._server_info = await self._safe_agent_info()
        self.event_adapter.thread_id = self.thread_id or "unknown"
        console.print("[green]Reconnected PostgreSQL target and started a new session.[/green]")
        print_connection_summary(build_db_connection_card(self.runtime_config, self._server_info))

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
            record = record_from_runtime(
                self.runtime_config,
                thread_id=self.thread_id,
                state_values=vals,
                server_info=self._server_info,
            )
            if has_resume_content(record):
                self.session_index.upsert(record)
        except Exception:
            pass

    async def _save_session_index_async(self):
        if not self.runtime_config.save_session or not self.thread_id:
            return
        vals = await self._current_values()
        try:
            record = record_from_runtime(
                self.runtime_config,
                thread_id=self.thread_id,
                state_values=vals,
                server_info=self._server_info,
            )
            if has_resume_content(record):
                self.session_index.upsert(record)
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
