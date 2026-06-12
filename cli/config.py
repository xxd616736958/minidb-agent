"""CLI runtime configuration and safe PostgreSQL connection metadata."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Optional
from urllib.parse import quote, urlparse, urlunparse

from execution.environment import (
    build_database_environment_profile,
    build_runtime_policy,
    build_workspace_profile,
)

OutputMode = Literal["human", "json", "jsonl"]
ApprovalMode = Literal["auto-readonly", "on-write", "always", "never"]

CLI_TARGET_ENVS = {"dev", "test", "staging", "prod", "production", "local", "unknown"}
STATE_TARGET_ENVS = {"dev", "staging", "production", "local", "unknown"}
SECRET_KEY_PARTS = ("password", "passwd", "secret", "token", "api_key", "apikey", "database_url")


@dataclass(frozen=True)
class CliRuntimeConfig:
    """Runtime options shared by interactive, exec, doctor, and session commands."""

    server_url: str = "http://localhost:2024"
    api_key: Optional[str] = None
    database_url: Optional[str] = None
    db_profile: Optional[str] = None
    target_environment: str = "unknown"
    readonly: bool = False
    approval_mode: ApprovalMode = "on-write"
    workspace: str = "."
    output_mode: OutputMode = "human"
    output_file: Optional[str] = None
    save_session: bool = True
    log_level: str = "WARNING"


def app_home() -> Path:
    return Path(os.environ.get("MINIDB_AGENT_HOME", "~/.minidb_agent")).expanduser()


def session_index_path() -> Path:
    return app_home() / "sessions.json"


def normalize_output_mode(value: str | None, *, json_flag: bool = False, jsonl_flag: bool = False) -> OutputMode:
    if jsonl_flag:
        return "jsonl"
    if json_flag:
        return "json"
    value = (value or "human").lower()
    if value not in {"human", "json", "jsonl"}:
        raise ValueError(f"Unsupported output mode: {value}")
    return value  # type: ignore[return-value]


def normalize_target_environment(value: str | None) -> str:
    raw = (value or os.environ.get("POSTGRES_TARGET_ENV") or "unknown").strip().lower()
    if raw == "production":
        return "prod"
    if raw not in CLI_TARGET_ENVS:
        return "unknown"
    return raw


def state_target_environment(value: str | None) -> str:
    raw = normalize_target_environment(value)
    if raw == "prod":
        return "production"
    # The AgentState schema does not currently have a separate "test" value.
    if raw == "test":
        return "staging"
    if raw in STATE_TARGET_ENVS:
        return raw
    return "unknown"


def database_url_from_env() -> Optional[str]:
    return os.environ.get("POSTGRES_TARGET_URL") or os.environ.get("POSTGRES_URI") or None


def runtime_config_from_args(args: Any) -> CliRuntimeConfig:
    """Build a runtime config from argparse output without leaking secrets."""

    return CliRuntimeConfig(
        server_url=getattr(args, "url", None) or os.environ.get("AGENT_SERVER_URL", "http://localhost:2024"),
        api_key=getattr(args, "api_key", None) or os.environ.get("AGENT_API_KEY") or None,
        database_url=getattr(args, "database_url", None) or database_url_from_env(),
        db_profile=getattr(args, "db_profile", None) or os.environ.get("POSTGRES_PROFILE") or None,
        target_environment=normalize_target_environment(getattr(args, "target_env", None)),
        readonly=bool(getattr(args, "readonly", False)),
        approval_mode=getattr(args, "approval_mode", None) or "on-write",
        workspace=str(Path(getattr(args, "workspace", None) or os.getcwd()).expanduser()),
        output_mode=normalize_output_mode(
            getattr(args, "output", None),
            json_flag=bool(getattr(args, "json", False)),
            jsonl_flag=bool(getattr(args, "jsonl", False)),
        ),
        output_file=getattr(args, "output_file", None),
        save_session=not bool(getattr(args, "no_save_session", False)),
        log_level=getattr(args, "log_level", None) or os.environ.get("AGENT_LOG_LEVEL", "WARNING"),
    )


def mask_database_url(database_url: str | None) -> Optional[str]:
    """Return a display-safe PostgreSQL URL with password removed."""

    if not database_url:
        return None
    parsed = urlparse(database_url)
    if not parsed.scheme or not parsed.netloc:
        return _mask_dsn_like_string(database_url)

    host = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port else ""
    auth = ""
    if parsed.username:
        auth = quote(parsed.username, safe="")
        if parsed.password:
            auth = f"{auth}:***"
        auth = f"{auth}@"
    safe_netloc = f"{auth}{host}{port}"
    return urlunparse((parsed.scheme, safe_netloc, parsed.path, parsed.params, parsed.query, parsed.fragment))


def _mask_dsn_like_string(value: str) -> str:
    parts = []
    for part in value.split():
        if "=" not in part:
            parts.append(part)
            continue
        key, raw = part.split("=", 1)
        if any(secret in key.lower() for secret in SECRET_KEY_PARTS):
            parts.append(f"{key}=***")
        else:
            parts.append(f"{key}={raw}")
    masked = " ".join(parts)
    return masked if masked else "***"


def build_db_connection_card(config: CliRuntimeConfig) -> dict[str, Any]:
    """Build a user-facing, credential-free database connection card."""

    parsed = urlparse(config.database_url or "")
    host = parsed.hostname or "not-configured"
    port = parsed.port or 5432
    database = parsed.path.lstrip("/") if parsed.path else "unknown"
    user = parsed.username or "unknown"
    display_url = mask_database_url(config.database_url) or "not-configured"
    fingerprint_source = "|".join(
        [
            host,
            str(port),
            database,
            user,
            normalize_target_environment(config.target_environment),
            config.db_profile or "",
        ]
    )
    fingerprint = hashlib.sha256(fingerprint_source.encode("utf-8")).hexdigest()[:16]
    return {
        "host": host,
        "port": port,
        "database": database,
        "user": user,
        "target_environment": normalize_target_environment(config.target_environment),
        "readonly": config.readonly,
        "approval_mode": config.approval_mode,
        "fingerprint": fingerprint,
        "display_url": display_url,
        "db_profile": config.db_profile,
    }


def build_agent_input_context(config: CliRuntimeConfig) -> dict[str, Any]:
    """Build safe AgentState fields from CLI options.

    The database URL itself is intentionally not included because LangGraph
    checkpoints may persist input state. Server-side tools should use server
    environment or managed profiles for credentials.
    """

    workspace_profile = build_workspace_profile(config.workspace)
    database_environment = build_database_environment_profile(config.database_url)
    env_name = state_target_environment(config.target_environment)
    is_production = env_name == "production"
    allow_writes = bool(database_environment.get("allow_write_tools"))
    if config.readonly or config.approval_mode == "never" or is_production or env_name == "unknown":
        allow_writes = False
    database_environment.update(
        {
            "environment_name": env_name,
            "access_mode": "read_only" if not allow_writes else "write_after_approval",
            "is_production": is_production,
            "allow_write_tools": allow_writes,
        }
    )
    runtime_policy = build_runtime_policy(database_environment)
    runtime_policy.update(
        {
            "allow_database_writes": allow_writes,
            "require_approval_for_database_write": allow_writes and config.approval_mode in {"auto-readonly", "on-write", "always"},
        }
    )
    return {
        "workspace_profile": workspace_profile,
        "database_environment": database_environment,
        "runtime_policy": runtime_policy,
    }


def safe_config_dict(config: CliRuntimeConfig) -> dict[str, Any]:
    """Return a JSON-safe config dict with secrets masked."""

    return {
        "server_url": config.server_url,
        "api_key": "***" if config.api_key else None,
        "database_url": mask_database_url(config.database_url),
        "db_profile": config.db_profile,
        "target_environment": config.target_environment,
        "readonly": config.readonly,
        "approval_mode": config.approval_mode,
        "workspace": config.workspace,
        "output_mode": config.output_mode,
        "output_file": config.output_file,
        "save_session": config.save_session,
        "log_level": config.log_level,
    }
