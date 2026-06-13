"""CLI entry point for the PostgreSQL management agent."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import warnings
from pathlib import Path
from typing import Any

warnings.filterwarnings("ignore", category=DeprecationWarning, module=r"langgraph\..*")
warnings.filterwarnings("ignore", message=r"The default value of `allowed_objects`.*")

from dotenv import load_dotenv

load_dotenv()

from cli.config import build_db_connection_card, persist_runtime_defaults, runtime_config_from_args
from cli.display import console, print_doctor_report, print_session_index
from cli.local_server import ensure_local_server
from cli.setup_flow import ensure_database_config
from cli.sessions import SessionIndex


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command line arguments."""

    parser = argparse.ArgumentParser(
        prog="minidb-agent",
        description="PostgreSQL management agent CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s
  %(prog)s --database-url "$POSTGRES_TARGET_URL" --target-env dev
  %(prog)s exec --json "检查 public.orders 表索引建议"
  %(prog)s doctor --database-url "$POSTGRES_TARGET_URL"
  %(prog)s sessions
        """,
    )
    _add_common_options(parser, suppress_defaults=False)
    subparsers = parser.add_subparsers(dest="command")

    exec_parser = subparsers.add_parser("exec", help="Run one non-interactive agent task")
    _add_common_options(exec_parser, suppress_defaults=True)
    exec_parser.add_argument("prompt", nargs="?", help="Prompt to run. Use '-' to read from stdin.")
    exec_parser.add_argument("--thread-id", default=None, help="Thread id to reuse for this exec run.")

    doctor_parser = subparsers.add_parser("doctor", help="Diagnose server, database, and workspace")
    _add_common_options(doctor_parser, suppress_defaults=True)
    doctor_parser.add_argument("--skip-server", action="store_true", help="Skip agent server health check.")
    doctor_parser.add_argument("--skip-database", action="store_true", help="Skip direct PostgreSQL connection check.")

    sessions_parser = subparsers.add_parser("sessions", help="List indexed CLI sessions")
    _add_common_options(sessions_parser, suppress_defaults=True)
    sessions_parser.add_argument("--all", action="store_true", help="Include archived sessions.")

    resume_parser = subparsers.add_parser("resume", help="Resume an indexed or explicit session")
    _add_common_options(resume_parser, suppress_defaults=True)
    resume_parser.add_argument("thread_id", nargs="?", help="Thread id to resume. Defaults to latest indexed session.")
    resume_parser.add_argument("--last", action="store_true", help="Resume latest indexed session without opening picker.")
    resume_parser.add_argument("--all", action="store_true", help="Include archived sessions in the picker.")

    return parser.parse_args(argv)


def _add_common_options(parser: argparse.ArgumentParser, *, suppress_defaults: bool) -> None:
    default = argparse.SUPPRESS if suppress_defaults else None
    parser.add_argument(
        "--url",
        default=argparse.SUPPRESS if suppress_defaults else os.environ.get("AGENT_SERVER_URL"),
        help="LangGraph server URL (default: http://127.0.0.1:2024)",
    )
    parser.add_argument(
        "--api-key",
        default=argparse.SUPPRESS if suppress_defaults else os.environ.get("AGENT_API_KEY"),
        help="API key for server authentication",
    )
    parser.add_argument("--database-url", default=argparse.SUPPRESS if suppress_defaults else os.environ.get("POSTGRES_TARGET_URL") or os.environ.get("POSTGRES_URI"), help="PostgreSQL target URL for CLI metadata and doctor checks")
    parser.add_argument("--db-profile", default=argparse.SUPPRESS if suppress_defaults else os.environ.get("POSTGRES_PROFILE"), help="Named PostgreSQL profile; credentials remain server-side")
    parser.add_argument("--target-env", default=argparse.SUPPRESS if suppress_defaults else os.environ.get("POSTGRES_TARGET_ENV", "unknown"), choices=["dev", "test", "staging", "prod", "production", "local", "unknown"], help="Target database environment")
    parser.add_argument("--readonly", action="store_true", default=default, help="Request readonly mode for this CLI session")
    parser.add_argument("--approval-mode", choices=["auto-readonly", "on-write", "always", "never"], default=argparse.SUPPRESS if suppress_defaults else os.environ.get("MINIDB_APPROVAL_MODE"), help="Database approval mode")
    parser.add_argument("--workspace", default=argparse.SUPPRESS if suppress_defaults else os.environ.get("MINIDB_WORKSPACE", os.getcwd()), help="Workspace directory")
    parser.add_argument("--output", choices=["human", "json", "jsonl"], default=argparse.SUPPRESS if suppress_defaults else os.environ.get("MINIDB_OUTPUT", "human"), help="Output mode")
    parser.add_argument("--json", action="store_true", default=default, help="Alias for --output json")
    parser.add_argument("--jsonl", action="store_true", default=default, help="Alias for --output jsonl")
    parser.add_argument("--output-file", default=argparse.SUPPRESS if suppress_defaults else None, help="Write exec output to file")
    parser.add_argument("--resume", default=argparse.SUPPRESS if suppress_defaults else None, help="Resume a specific session by thread_id")
    parser.add_argument("--new", action="store_true", default=default, help="Force a new interactive session")
    parser.add_argument("--no-save-session", action="store_true", default=default, help="Do not update local CLI session index")
    parser.add_argument("--log-level", default=argparse.SUPPRESS if suppress_defaults else os.environ.get("AGENT_LOG_LEVEL", "WARNING"), choices=["DEBUG", "INFO", "WARNING", "ERROR"], help="Logging level")
    parser.add_argument("--verbose", action="store_true", default=default, help="Show internal planning, routing, and delivery events.")
    parser.add_argument("--no-server", action="store_true", default=default, help="Do not auto-start a local LangGraph server.")


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.WARNING),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


async def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config = runtime_config_from_args(args)
    setup_logging(config.log_level)

    if args.command == "exec":
        from cli.exec_mode import run_exec

        config = ensure_database_config(config, interactive=False)
        persist_runtime_defaults(config)
        if not getattr(args, "no_server", False):
            try:
                config = await ensure_local_server(config)
                persist_runtime_defaults(config)
            except Exception as exc:
                console.print(f"[red]Cannot start local server: {exc}[/red]")
                return 1
        return await run_exec(config, args.prompt, thread_id=getattr(args, "thread_id", None) or getattr(args, "resume", None))

    if args.command == "doctor":
        from cli.doctor import doctor_exit_code, run_doctor

        report = await run_doctor(config, check_server=not args.skip_server, check_database=not args.skip_database)
        if config.output_mode == "human":
            print_doctor_report(report)
        else:
            _print_json(report)
        return doctor_exit_code(report)

    if args.command == "sessions":
        records = SessionIndex().list(include_archived=bool(args.all), limit=50)
        if config.output_mode == "human":
            print_session_index(records)
        else:
            _print_json({"sessions": records})
        return 0

    if args.command == "resume":
        thread_id = args.thread_id
        if not thread_id and getattr(args, "last", False):
            thread_id = _latest_thread_id(config)
        if not thread_id:
            thread_id = await _pick_thread_id(config, include_archived=bool(getattr(args, "all", False)))
        if not thread_id:
            console.print("[red]No session selected.[/red]")
            return 2
        args.resume = thread_id

    return await _run_repl(args, config)


async def _run_repl(args: argparse.Namespace, config: Any) -> int:
    from cli.repl import AgentRepl

    thread_id = _select_thread_id(args, config)
    config = ensure_database_config(config, interactive=True)
    persist_runtime_defaults(config)
    if not getattr(args, "no_server", False):
        try:
            config = await ensure_local_server(config)
            persist_runtime_defaults(config)
        except Exception as exc:
            console.print(f"[red]Cannot start local server: {exc}[/red]")
            return 1
    if not await _validate_server(config):
        return 1

    repl = AgentRepl(
        server_url=config.server_url,
        api_key=config.api_key,
        thread_id=thread_id,
        runtime_config=config,
    )

    try:
        await repl.run()
    except Exception as exc:
        console.print(f"[red]Fatal error: {exc}[/red]")
        return 1
    return 0


def _select_thread_id(args: argparse.Namespace, config: Any) -> str | None:
    if getattr(args, "resume", None):
        return args.resume
    # Match Codex/Claude session semantics: starting the CLI opens a fresh
    # conversation unless the user explicitly resumes one.
    return None


def _latest_thread_id(config: Any) -> str | None:
    record = SessionIndex().latest_for_config(config)
    return str(record.get("thread_id")) if record else None


async def _pick_thread_id(config: Any, *, include_archived: bool = False) -> str | None:
    from cli.session_picker import choose_session

    project_dir = str(Path(config.workspace).expanduser().resolve())
    index = SessionIndex()
    records = index.list(include_archived=include_archived, project_dir=project_dir, limit=30)
    if not records:
        records = index.list(include_archived=include_archived, limit=30)
    return await choose_session(records)


async def _validate_server(config: Any) -> bool:
    if config.verbose:
        console.print(f"[dim]Connecting to {config.server_url}...[/dim]")
    try:
        import httpx

        async with httpx.AsyncClient(timeout=5) as client:
            api_key_header = {"X-API-Key": config.api_key} if config.api_key else {}
            resp = await client.get(f"{config.server_url}/health", headers=api_key_header)
            if resp.status_code == 200:
                if config.verbose:
                    console.print("[green]✓ Server healthy[/green]")
                return True
            if resp.status_code == 401:
                console.print("[red]✗ Authentication failed - check your API key[/red]")
                return False
            console.print(f"[yellow]⚠ Server returned {resp.status_code}[/yellow]")
            return True
    except httpx.ConnectError:
        console.print(
            f"[red]✗ Cannot connect to {config.server_url}[/red]\n"
            "[dim]Is the server running? Start it with: make dev[/dim]"
        )
        return False
    except Exception as exc:
        console.print(f"[yellow]⚠ Could not verify server: {exc}[/yellow]")
        return True


def _print_json(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, default=str))


def sync_main() -> None:
    """Console-script entry point."""
    sys.exit(asyncio.run(main()))


if __name__ == "__main__":
    sync_main()
