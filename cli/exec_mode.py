"""Non-interactive CLI execution mode."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Callable, Optional

from langgraph_sdk import get_client

from cli.config import CliRuntimeConfig, build_agent_input_context
from cli.events import CliEventAdapter, event_to_json, sanitize_for_cli
from cli.server_info import fetch_agent_info
from cli.sessions import SessionIndex, record_from_runtime
from cli.display import console, print_cli_event, print_error


async def run_exec(
    config: CliRuntimeConfig,
    prompt: str | None,
    *,
    thread_id: str | None = None,
    client_factory: Callable[..., Any] = get_client,
) -> int:
    """Run one non-interactive agent turn and emit human/json/jsonl output."""

    prompt = _resolve_prompt(prompt)
    if not prompt:
        print_error("No prompt provided for exec mode.")
        return 2

    client = client_factory(url=config.server_url, api_key=config.api_key)
    assistant_id = await _resolve_assistant_id(client)
    thread_id = thread_id or await _create_thread(client)
    adapter = CliEventAdapter(thread_id)
    events: list[dict[str, Any]] = []
    last_values: dict[str, Any] = {}
    run_error: dict[str, Any] | None = None
    server_info = await fetch_agent_info(config)
    if config.output_file and config.output_mode in {"json", "jsonl"}:
        _write_output(config, "", append=False)

    try:
        stream_input = {
            "messages": [{"role": "user", "content": prompt}],
            **build_agent_input_context(config, server_info),
        }
        async for event in client.runs.stream(
            thread_id=thread_id,
            assistant_id=assistant_id,
            input=stream_input,
            stream_mode="updates",
            config={"recursion_limit": config.recursion_limit},
        ):
            data = getattr(event, "data", event)
            if not isinstance(data, dict):
                continue
            for cli_event in adapter.events_from_stream_data(data):
                events.append(cli_event)
                _emit_event(config, cli_event)
        try:
            state = await client.threads.get_state(thread_id)
            last_values = state.get("values", {}) if state else {}
        except Exception:
            last_values = {}
        run_error = await _latest_run_error(client, thread_id)
    except Exception as exc:
        if config.output_mode == "human":
            print_error(f"Agent exec failed: {exc}")
        else:
            _write_output(config, event_to_json({"type": "error", "message": str(exc), "thread_id": thread_id}) + "\n", append=True)
        return 1

    if run_error:
        if config.output_mode == "human":
            print_error(_run_error_message(run_error))
        elif config.output_mode == "jsonl":
            _write_output(
                config,
                event_to_json({"type": "error", "thread_id": thread_id, "message": _run_error_message(run_error)}) + "\n",
                append=True,
            )
        if config.output_mode == "json":
            last_values = {**last_values, "run_error": run_error}

    if config.save_session:
        SessionIndex().upsert(
            record_from_runtime(config, thread_id=thread_id, state_values=last_values, server_info=server_info)
        )
    if config.output_mode == "json":
        _emit_json_result(config, thread_id, events, last_values)
    return 1 if run_error else _exit_code_from_state(last_values, events)


def _resolve_prompt(prompt: Optional[str]) -> str:
    if prompt == "-":
        return sys.stdin.read()
    if prompt:
        return prompt
    if not sys.stdin.isatty():
        return sys.stdin.read()
    return ""


async def _resolve_assistant_id(client: Any) -> str:
    try:
        assistants = await client.assistants.search()
        if assistants:
            return assistants[0].get("assistant_id", assistants[0].get("name", "agent"))
    except Exception:
        pass
    return "agent"


async def _create_thread(client: Any) -> str:
    thread = await client.threads.create()
    return thread["thread_id"]


def _emit_event(config: CliRuntimeConfig, event: dict[str, Any]) -> None:
    if config.output_mode == "human":
        print_cli_event(event, verbose=True)
    elif config.output_mode == "jsonl":
        _write_output(config, event_to_json(event) + "\n", append=True)


def _emit_json_result(config: CliRuntimeConfig, thread_id: str, events: list[dict[str, Any]], values: dict[str, Any]) -> None:
    payload = {
        "thread_id": thread_id,
        "status": _status_from_state(values, events),
        "events": sanitize_for_cli(events),
        "delivery_packages": sanitize_for_cli(values.get("delivery_packages") or []),
        "artifact_manifests": sanitize_for_cli(values.get("artifact_manifests") or []),
        "run_error": sanitize_for_cli(values.get("run_error")),
    }
    _write_output(config, event_to_json(payload) + "\n", append=False)


def _write_output(config: CliRuntimeConfig, text: str, *, append: bool) -> None:
    if config.output_file:
        path = Path(config.output_file).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        mode = "a" if append else "w"
        with path.open(mode, encoding="utf-8") as fh:
            fh.write(text)
        return
    console.print(text, end="")


def _status_from_state(values: dict[str, Any], events: list[dict[str, Any]]) -> str:
    if values.get("run_error"):
        return "error"
    runtime = values.get("db_task_runtime") or {}
    if runtime.get("task_status"):
        return str(runtime["task_status"])
    packages = values.get("delivery_packages") or []
    if packages and packages[-1].get("status") == "ready":
        return "completed"
    if events:
        return str(events[-1].get("type"))
    return "unknown"


def _exit_code_from_state(values: dict[str, Any], events: list[dict[str, Any]]) -> int:
    status = _status_from_state(values, events)
    if status in {"completed", "ready", "delivery_ready"}:
        return 0
    packages = values.get("delivery_packages") or []
    if packages and packages[-1].get("status") == "ready":
        return 0
    if status in {"failed", "error"}:
        return 1
    if status == "blocked" or any(event.get("type") == "blocked" for event in events):
        return 3
    return 0


async def _latest_run_error(client: Any, thread_id: str) -> dict[str, Any] | None:
    try:
        runs = await client.runs.list(thread_id, limit=1)
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
            detail = await client.runs.join(thread_id, run_id)
        except Exception:
            detail = None
    if isinstance(detail, dict) and detail.get("__error__"):
        return {"run_id": run_id, **detail["__error__"]}
    return {
        "run_id": run_id,
        "error": str(latest.get("status") or "error"),
        "message": "Agent run failed before reaching a terminal state.",
    }


def _run_error_message(error: dict[str, Any]) -> str:
    kind = str(error.get("error") or "AgentRunError")
    message = str(error.get("message") or "Agent run failed.")
    return f"{kind}: {message}"
