"""Local LangGraph server lifecycle for Codex-style one-command startup."""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from cli.config import CliRuntimeConfig, app_home, build_db_connection_card, normalize_target_environment
from cli.server_info import fetch_agent_info


PROJECT_ROOT = Path(__file__).resolve().parents[1]

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _server_state_path() -> Path:
    return app_home() / "server.json"


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _load_state() -> dict[str, Any]:
    path = _server_state_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _save_state(state: dict[str, Any]) -> None:
    path = _server_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def _port_open(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex((host, port)) == 0


def _next_port(preferred: int) -> int:
    for port in range(preferred, preferred + 30):
        if not _port_open("127.0.0.1", port):
            return port
    raise RuntimeError("No free local port found for MiniDB Agent server.")


def _url_for_port(port: int) -> str:
    return f"http://127.0.0.1:{port}"


def _parse_port(server_url: str) -> int:
    parsed = urlparse(server_url)
    return parsed.port or 2024


def _target_matches(config: CliRuntimeConfig, server_info: dict[str, Any] | None) -> bool:
    if not config.database_url:
        return True
    if not server_info:
        return False
    desired = build_db_connection_card(config)
    actual = build_db_connection_card(config, server_info)
    keys = ("host", "port", "database", "user")
    return all(str(desired.get(key)) == str(actual.get(key)) for key in keys)


def _langgraph_binary() -> str:
    candidate = Path(sys.executable).with_name("langgraph")
    if candidate.exists():
        return str(candidate)
    return "langgraph"


def _server_env(config: CliRuntimeConfig) -> dict[str, str]:
    env = os.environ.copy()
    if config.database_url:
        env["POSTGRES_TARGET_URL"] = config.database_url
    if config.target_environment:
        env["POSTGRES_TARGET_ENV"] = normalize_target_environment(config.target_environment)
    if config.log_level:
        env["AGENT_LOG_LEVEL"] = config.log_level
    return env


async def _health_ok(url: str, api_key: str | None) -> bool:
    try:
        headers = {"X-API-Key": api_key} if api_key else {}
        async with httpx.AsyncClient(timeout=2) as client:
            resp = await client.get(f"{url}/health", headers=headers)
            return resp.status_code == 200
    except Exception:
        return False


async def _wait_until_ready(url: str, api_key: str | None, *, timeout_seconds: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if await _health_ok(url, api_key):
            return True
        await asyncio.sleep(0.4)
    return False


def stop_managed_server() -> None:
    """Stop the CLI-managed server if it is still running."""

    state = _load_state()
    pid = state.get("pid")
    if not _pid_alive(pid):
        return
    try:
        os.kill(int(pid), signal.SIGTERM)
    except OSError:
        return


async def ensure_local_server(config: CliRuntimeConfig, *, force_restart: bool = False) -> CliRuntimeConfig:
    """Ensure a local LangGraph server exists and matches the configured database."""

    existing_info = await fetch_agent_info(config)
    if existing_info and not force_restart and _target_matches(config, existing_info):
        return config

    state = _load_state()
    managed_pid = int(state.get("pid") or 0)
    managed_url = str(state.get("url") or "")
    if force_restart or (existing_info and managed_url == config.server_url and _pid_alive(managed_pid)):
        stop_managed_server()
        time.sleep(0.5)

    preferred_port = _parse_port(config.server_url)
    port = preferred_port if not _port_open("127.0.0.1", preferred_port) else _next_port(preferred_port + 1)
    server_url = _url_for_port(port)
    log_path = app_home() / "server.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = log_path.open("ab")

    proc = subprocess.Popen(
        [
            _langgraph_binary(),
            "dev",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--no-browser",
        ],
        cwd=str(PROJECT_ROOT),
        env=_server_env(config),
        stdout=log_file,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    log_file.close()
    new_config = replace(config, server_url=server_url)
    _save_state(
        {
            "pid": proc.pid,
            "url": server_url,
            "port": port,
            "project_root": str(PROJECT_ROOT),
            "database_fingerprint": build_db_connection_card(new_config)["fingerprint"],
            "started_at": _now_iso(),
            "log_path": str(log_path),
        }
    )
    if not await _wait_until_ready(server_url, config.api_key):
        raise RuntimeError(f"MiniDB Agent server did not become ready. See {log_path}")
    return new_config
