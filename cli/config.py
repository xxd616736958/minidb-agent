"""CLI runtime configuration and safe PostgreSQL connection metadata."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Optional
from urllib.parse import quote, urlparse, urlunparse

OutputMode = Literal["human", "json", "jsonl"]
ApprovalMode = Literal["auto-readonly", "on-write", "always", "never"]

CLI_TARGET_ENVS = {"dev", "test", "staging", "prod", "production", "local", "unknown"}
STATE_TARGET_ENVS = {"dev", "staging", "production", "local", "unknown"}
SECRET_KEY_PARTS = ("password", "passwd", "secret", "token", "api_key", "apikey", "database_url")


@dataclass(frozen=True)
class CliRuntimeConfig:
    """Runtime options shared by interactive, exec, doctor, and session commands."""

    server_url: str = "http://127.0.0.1:2024"
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
    verbose: bool = False
    recursion_limit: int = 80


def app_home() -> Path:
    return Path(os.environ.get("MINIDB_AGENT_HOME", "~/.minidb_agent")).expanduser()


def user_config_path() -> Path:
    return app_home() / "config.json"


def session_index_path() -> Path:
    return app_home() / "sessions.json"


def load_user_config() -> dict[str, Any]:
    path = user_config_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def save_user_config(update: dict[str, Any]) -> dict[str, Any]:
    path = user_config_path()
    data = load_user_config()
    data.update({key: value for key, value in update.items() if value is not None})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return data


def persist_runtime_defaults(config: CliRuntimeConfig) -> None:
    update = {
        "database_url": config.database_url,
        "target_environment": normalize_target_environment(config.target_environment),
        "approval_mode": config.approval_mode,
    }
    if config.server_url:
        update["server_url"] = config.server_url
    save_user_config(update)


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


def _is_valid_state_environment(value: Any) -> bool:
    return str(value or "") in STATE_TARGET_ENVS


def _server_database_environment(server_info: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(server_info, dict):
        return {}
    env = server_info.get("database_environment")
    return env if isinstance(env, dict) else {}


def _server_target_configured(server_info: dict[str, Any] | None) -> bool:
    if not isinstance(server_info, dict):
        return False
    postgres = server_info.get("postgres")
    if isinstance(postgres, dict) and "target_configured" in postgres:
        return bool(postgres.get("target_configured"))
    env = _server_database_environment(server_info)
    return bool(env.get("target_database") or env.get("safe_host_label"))


def _safe_server_port(server_env: dict[str, Any]) -> int | None:
    port = server_env.get("safe_port")
    try:
        return int(port) if port is not None else None
    except (TypeError, ValueError):
        return None


def _environment_from_text(text: str) -> str:
    lowered = text.lower()
    if "prod" in lowered or "production" in lowered:
        return "production"
    if "stag" in lowered:
        return "staging"
    if "dev" in lowered:
        return "dev"
    if "local" in lowered or "127.0.0.1" in lowered or "localhost" in lowered:
        return "local"
    return "unknown"


def database_url_from_env() -> Optional[str]:
    return os.environ.get("POSTGRES_TARGET_URL") or os.environ.get("POSTGRES_URI") or None


def runtime_config_from_args(args: Any) -> CliRuntimeConfig:
    """Build a runtime config from argparse output without leaking secrets."""

    stored = load_user_config()
    env_database_url = database_url_from_env()
    arg_database_url = getattr(args, "database_url", None)
    arg_target_env = getattr(args, "target_env", None)
    env_target_env = os.environ.get("POSTGRES_TARGET_ENV")
    stored_target_env = stored.get("target_environment")
    target_env = arg_target_env or env_target_env or stored_target_env or "unknown"
    if target_env == "unknown" and stored_target_env:
        target_env = stored_target_env

    return CliRuntimeConfig(
        server_url=getattr(args, "url", None)
        or os.environ.get("AGENT_SERVER_URL")
        or stored.get("server_url")
        or "http://127.0.0.1:2024",
        api_key=getattr(args, "api_key", None) or os.environ.get("AGENT_API_KEY") or None,
        database_url=arg_database_url or env_database_url or stored.get("database_url"),
        db_profile=getattr(args, "db_profile", None) or os.environ.get("POSTGRES_PROFILE") or None,
        target_environment=normalize_target_environment(target_env),
        readonly=bool(getattr(args, "readonly", False)),
        approval_mode=getattr(args, "approval_mode", None) or stored.get("approval_mode") or "on-write",
        workspace=str(Path(getattr(args, "workspace", None) or os.getcwd()).expanduser()),
        output_mode=normalize_output_mode(
            getattr(args, "output", None),
            json_flag=bool(getattr(args, "json", False)),
            jsonl_flag=bool(getattr(args, "jsonl", False)),
        ),
        output_file=getattr(args, "output_file", None),
        save_session=not bool(getattr(args, "no_save_session", False)),
        log_level=getattr(args, "log_level", None) or os.environ.get("AGENT_LOG_LEVEL", "WARNING"),
        verbose=bool(getattr(args, "verbose", False)) or os.environ.get("MINIDB_VERBOSE", "").lower() in {"1", "true", "yes", "on"},
        recursion_limit=int(os.environ.get("MINIDB_RECURSION_LIMIT", "80")),
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


def build_db_connection_card(config: CliRuntimeConfig, server_info: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build a user-facing, credential-free database connection card."""

    parsed = urlparse(config.database_url or "")
    server_env = _server_database_environment(server_info)
    use_server_target = _server_target_configured(server_info)
    host = (server_env.get("safe_host_label") if use_server_target else None) or parsed.hostname or "not-configured"
    port = _safe_server_port(server_env) or parsed.port or 5432
    database = (
        (server_env.get("target_database") if use_server_target else None)
        or (parsed.path.lstrip("/") if parsed.path else None)
        or "unknown"
    )
    user = (server_env.get("safe_user_label") if use_server_target else None) or parsed.username or "unknown"
    server_env_name = str(server_env.get("environment_name") or "unknown")
    config_env = normalize_target_environment(config.target_environment)
    if config_env in {"prod", "production"} or server_env_name == "production":
        env = "prod"
    elif use_server_target and server_env_name in {"dev", "staging", "local"}:
        env = server_env_name
    else:
        env = config_env
    display_url = "server env:POSTGRES_TARGET_URL" if use_server_target else mask_database_url(config.database_url) or "not-configured"
    fingerprint_source = "|".join(
        [
            host,
            str(port),
            database,
            user,
            env,
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
        "source": "server" if use_server_target else "cli",
    }


def build_agent_input_context(
    config: CliRuntimeConfig,
    server_info: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build safe AgentState fields from CLI options.

    The database URL itself is intentionally not included because LangGraph
    checkpoints may persist input state. Server-side tools should use server
    environment or managed profiles for credentials.
    """

    root = Path(config.workspace or os.getcwd()).expanduser().resolve()
    mini_root = root / ".mini_agent"
    workspace_profile = {
        "root_path": str(root),
        "read_allowed_paths": [str(root)],
        "write_allowed_paths": [str(root)],
        "artifact_root": str(mini_root / "artifacts"),
        "report_root": str(mini_root / "reports"),
        "temp_root": str(mini_root / "tmp"),
        "default_cwd": str(root),
        "git_repo": str(root / ".git") if (root / ".git").exists() else None,
        "dirty_state_known": False,
    }

    database_url = config.database_url or database_url_from_env()
    parsed = urlparse(database_url or "")
    env_from_url = _environment_from_text(database_url or "") if database_url else "unknown"
    server_env = _server_database_environment(server_info)
    use_server_target = _server_target_configured(server_info)
    config_env_name = state_target_environment(config.target_environment)
    url_env_name = state_target_environment(env_from_url)
    server_env_name = str(server_env.get("environment_name") or "unknown")
    if not _is_valid_state_environment(server_env_name):
        server_env_name = "unknown"
    if "production" in {config_env_name, server_env_name}:
        env_name = "production"
    elif use_server_target and server_env_name != "unknown":
        env_name = server_env_name
    elif config_env_name != "unknown":
        env_name = config_env_name
    elif url_env_name != "unknown":
        env_name = url_env_name
    else:
        env_name = "unknown"
    is_production = env_name == "production"
    is_unknown = env_name == "unknown"
    target_database = (
        (server_env.get("target_database") if use_server_target else None)
        or (parsed.path.lstrip("/") if parsed.path else None)
    )
    safe_host = (server_env.get("safe_host_label") if use_server_target else None) or parsed.hostname
    safe_user = (server_env.get("safe_user_label") if use_server_target else None) or parsed.username
    database_environment = {
        "environment_name": env_name,
        "target_database": target_database,
        "safe_host_label": safe_host,
        "safe_user_label": safe_user,
        "access_mode": "read_only" if is_production or is_unknown else "write_after_approval",
        "is_production": is_production,
        "default_statement_timeout_ms": int(server_env.get("default_statement_timeout_ms") or os.environ.get("POSTGRES_STATEMENT_TIMEOUT_MS", "30000")),
        "default_lock_timeout_ms": int(server_env.get("default_lock_timeout_ms") or os.environ.get("POSTGRES_LOCK_TIMEOUT_MS", "5000")),
        "max_result_rows": int(server_env.get("max_result_rows") or os.environ.get("POSTGRES_MAX_RESULT_ROWS", "100")),
        "allow_write_tools": not (is_production or is_unknown),
        "require_backup_check_for_writes": is_production or is_unknown,
        "credential_ref": server_env.get("credential_ref")
        or ("env:POSTGRES_TARGET_URL" if os.environ.get("POSTGRES_TARGET_URL") else "settings:POSTGRES_URI"),
    }
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
    runtime_policy = {
        "allow_shell_database_clients": False,
        "allow_network_tools": True,
        "allow_file_writes": True,
        "allow_database_writes": allow_writes,
        "require_approval_for_workspace_write": False,
        "require_approval_for_database_write": allow_writes and config.approval_mode in {"auto-readonly", "on-write", "always"},
        "max_tool_duration_seconds": 120,
        "max_artifact_size_bytes": 2_000_000,
    }
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
        "verbose": config.verbose,
        "recursion_limit": config.recursion_limit,
    }
