"""Execution environment, workspace boundary, and artifact helpers."""

from __future__ import annotations

import os
import shlex
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from agent.config import get_settings
from agent.state import (
    AgentState,
    ArtifactRecord,
    DatabaseEnvironmentProfile,
    RuntimePolicy,
    TaskWorkspace,
    WorkspaceProfile,
)


DATABASE_CLIENT_COMMANDS = {
    "psql",
    "pg_dump",
    "pg_restore",
    "createdb",
    "dropdb",
    "createuser",
    "dropuser",
    "vacuumdb",
    "reindexdb",
    "clusterdb",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_resolve(path: str | Path, base: str | Path | None = None) -> Path:
    p = Path(path).expanduser()
    if not p.is_absolute() and base is not None:
        p = Path(base) / p
    return p.resolve()


def _is_within(path: Path, roots: list[str]) -> bool:
    for root in roots:
        root_path = Path(root).expanduser().resolve()
        try:
            path.relative_to(root_path)
            return True
        except ValueError:
            continue
    return False


def build_workspace_profile(root_path: str | None = None) -> WorkspaceProfile:
    root = _safe_resolve(root_path or os.getcwd())
    mini_root = root / ".mini_agent"
    artifact_root = mini_root / "artifacts"
    report_root = mini_root / "reports"
    temp_root = mini_root / "tmp"
    return {
        "root_path": str(root),
        "read_allowed_paths": [str(root)],
        "write_allowed_paths": [str(root)],
        "artifact_root": str(artifact_root),
        "report_root": str(report_root),
        "temp_root": str(temp_root),
        "default_cwd": str(root),
        "git_repo": str(root / ".git") if (root / ".git").exists() else None,
        "dirty_state_known": False,
    }


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


def build_database_environment_profile(database_url: str | None = None) -> DatabaseEnvironmentProfile:
    settings = get_settings()
    url = database_url or os.environ.get("POSTGRES_TARGET_URL") or settings.postgres_uri
    parsed = urlparse(url) if url else None
    env_name = os.environ.get("POSTGRES_TARGET_ENV", "")
    if not env_name and url:
        env_name = _environment_from_text(url)
    env_name = env_name if env_name in {"local", "dev", "staging", "production"} else "unknown"
    is_production = env_name == "production"
    is_unknown = env_name == "unknown"
    access_mode = "read_only" if is_production or is_unknown else "write_after_approval"
    return {
        "environment_name": env_name,  # type: ignore[typeddict-item]
        "target_database": parsed.path.lstrip("/") if parsed and parsed.path else None,
        "safe_host_label": parsed.hostname if parsed else None,
        "safe_user_label": parsed.username if parsed else None,
        "access_mode": access_mode,  # type: ignore[typeddict-item]
        "is_production": is_production,
        "default_statement_timeout_ms": int(os.environ.get("POSTGRES_STATEMENT_TIMEOUT_MS", "30000")),
        "default_lock_timeout_ms": int(os.environ.get("POSTGRES_LOCK_TIMEOUT_MS", "5000")),
        "max_result_rows": int(os.environ.get("POSTGRES_MAX_RESULT_ROWS", "100")),
        "allow_write_tools": not (is_production or is_unknown),
        "require_backup_check_for_writes": is_production or is_unknown,
        "credential_ref": "env:POSTGRES_TARGET_URL" if os.environ.get("POSTGRES_TARGET_URL") else "settings:POSTGRES_URI",
    }


def build_runtime_policy(database_environment: DatabaseEnvironmentProfile | None = None) -> RuntimePolicy:
    env = database_environment or build_database_environment_profile()
    return {
        "allow_shell_database_clients": False,
        "allow_network_tools": True,
        "allow_file_writes": True,
        "allow_database_writes": bool(env.get("allow_write_tools")),
        "require_approval_for_workspace_write": False,
        "require_approval_for_database_write": True,
        "max_tool_duration_seconds": 120,
        "max_artifact_size_bytes": 2_000_000,
    }


class WorkspaceManager:
    """Validate workspace paths against read/write boundaries."""

    def __init__(self, profile: WorkspaceProfile | None = None) -> None:
        self.profile = profile or build_workspace_profile()

    def resolve_for_read(self, path: str) -> Path:
        resolved = _safe_resolve(path, self.profile["default_cwd"])
        if not _is_within(resolved, self.profile["read_allowed_paths"]):
            raise PermissionError(f"Read path is outside workspace boundary: {resolved}")
        return resolved

    def resolve_for_write(self, path: str) -> Path:
        resolved = _safe_resolve(path, self.profile["default_cwd"])
        if not _is_within(resolved, self.profile["write_allowed_paths"]):
            raise PermissionError(f"Write path is outside workspace boundary: {resolved}")
        return resolved

    def ensure_base_dirs(self) -> None:
        for key in ("artifact_root", "report_root", "temp_root"):
            Path(self.profile[key]).mkdir(parents=True, exist_ok=True)


class DatabaseEnvironmentManager:
    """Build and expose safe PostgreSQL target environment metadata."""

    def __init__(self, profile: DatabaseEnvironmentProfile | None = None) -> None:
        self.profile = profile or build_database_environment_profile()

    @property
    def safe_summary(self) -> dict[str, Any]:
        return {
            "environment_name": self.profile["environment_name"],
            "target_database": self.profile["target_database"],
            "safe_host_label": self.profile["safe_host_label"],
            "safe_user_label": self.profile["safe_user_label"],
            "access_mode": self.profile["access_mode"],
            "is_production": self.profile["is_production"],
        }

    def readonly_session(self) -> dict[str, Any]:
        return {
            "mode": "read_only",
            "readonly": True,
            "requires_approval": False,
            "statement_timeout_ms": self.profile["default_statement_timeout_ms"],
            "lock_timeout_ms": self.profile["default_lock_timeout_ms"],
            "max_result_rows": self.profile["max_result_rows"],
        }

    def diagnostic_session(self) -> dict[str, Any]:
        return {
            "mode": "diagnostic",
            "readonly": True,
            "requires_approval": False,
            "allowed_operations": ["explain", "pg_stat", "lock_inspect", "schema_inspect"],
            "statement_timeout_ms": self.profile["default_statement_timeout_ms"],
            "lock_timeout_ms": self.profile["default_lock_timeout_ms"],
            "max_result_rows": self.profile["max_result_rows"],
        }

    def write_session(
        self,
        *,
        approval_id: str,
        sql_hash: str,
        rollback_summary: str,
        verification_criteria: list[str],
    ) -> dict[str, Any]:
        missing = [
            name
            for name, value in {
                "approval_id": approval_id,
                "sql_hash": sql_hash,
                "rollback_summary": rollback_summary,
                "verification_criteria": verification_criteria,
            }.items()
            if not value
        ]
        if missing:
            raise ValueError(f"Write session requires: {', '.join(missing)}")
        if not self.profile.get("allow_write_tools"):
            raise PermissionError(
                "Write session is disabled for "
                f"target environment '{self.profile.get('environment_name')}'."
            )
        return {
            "mode": "write_after_approval",
            "readonly": False,
            "requires_approval": True,
            "approval_id": approval_id,
            "sql_hash": sql_hash,
            "rollback_summary": rollback_summary,
            "verification_criteria": verification_criteria,
            "statement_timeout_ms": self.profile["default_statement_timeout_ms"],
            "lock_timeout_ms": self.profile["default_lock_timeout_ms"],
            "max_result_rows": self.profile["max_result_rows"],
        }


class TaskWorkspaceManager:
    """Create per-task workspace records and directories."""

    def __init__(self, workspace: WorkspaceProfile | None = None) -> None:
        self.workspace = workspace or build_workspace_profile()

    def build(self, state: AgentState | None = None) -> TaskWorkspace:
        state = state or {}
        intent = state.get("current_intent") or {}
        plan = state.get("db_task_plan") or {}
        task_id = str(plan.get("id") or intent.get("id") or f"task-{uuid.uuid4().hex[:12]}")
        root = Path(self.workspace["root_path"]) / ".mini_agent" / "tasks" / task_id
        for name in ("sql", "explain", "health", "approvals", "logs", "reports"):
            (root / name).mkdir(parents=True, exist_ok=True)
        existing = state.get("task_workspace") or {}
        created_at = existing.get("created_at") or now_iso()
        return {
            "task_id": task_id,
            "intent_id": intent.get("id"),
            "plan_id": plan.get("id"),
            "root_path": str(root),
            "artifact_ids": list(existing.get("artifact_ids", [])),
            "report_paths": list(existing.get("report_paths", [])),
            "sql_draft_paths": list(existing.get("sql_draft_paths", [])),
            "execution_log_ref": existing.get("execution_log_ref"),
            "created_at": created_at,
            "updated_at": now_iso(),
        }


class ArtifactStore:
    """Create lightweight artifact metadata records."""

    def __init__(self, task_workspace: TaskWorkspace | None = None) -> None:
        self.task_workspace = task_workspace

    def record(
        self,
        *,
        kind: str,
        summary: str,
        path: str | None = None,
        payload_ref: str | None = None,
        sensitivity: str = "internal",
        lifecycle: str = "session",
    ) -> ArtifactRecord:
        task_id = (self.task_workspace or {}).get("task_id", "task-unknown")
        return {
            "id": f"artifact-{uuid.uuid4().hex[:12]}",
            "task_id": task_id,
            "kind": kind,  # type: ignore[typeddict-item]
            "path": path,
            "payload_ref": payload_ref,
            "summary": summary,
            "sensitivity": sensitivity,  # type: ignore[typeddict-item]
            "lifecycle": lifecycle,  # type: ignore[typeddict-item]
            "created_at": now_iso(),
        }


class ExecutionEnvironmentManager:
    """Facade for workspace, database environment, task workspace, and runtime policy."""

    def __init__(self, state: AgentState | None = None) -> None:
        self.state = state or {}
        self.workspace_profile = self.state.get("workspace_profile") or build_workspace_profile()
        self.database_environment = self.state.get("database_environment") or build_database_environment_profile()
        self.runtime_policy = self.state.get("runtime_policy") or build_runtime_policy(self.database_environment)
        self.task_workspace = self.state.get("task_workspace")
        self.workspace = WorkspaceManager(self.workspace_profile)
        self.database = DatabaseEnvironmentManager(self.database_environment)

    def bootstrap_state(self) -> dict[str, Any]:
        self.workspace.ensure_base_dirs()
        task_workspace = self.state.get("task_workspace") or TaskWorkspaceManager(self.workspace_profile).build(self.state)
        self.task_workspace = task_workspace
        return {
            "workspace_profile": self.workspace_profile,
            "database_environment": self.database_environment,
            "runtime_policy": self.runtime_policy,
            "task_workspace": task_workspace,
        }

    def resolve_read_path(self, path: str) -> Path:
        return self.workspace.resolve_for_read(path)

    def resolve_write_path(self, path: str) -> Path:
        return self.workspace.resolve_for_write(path)

    def shell_command_allowed(self, command: str) -> tuple[bool, str | None]:
        try:
            parts = shlex.split(command)
        except ValueError as exc:
            return False, f"Invalid shell command: {exc}"
        if not parts:
            return False, "Empty shell command."
        base = Path(parts[0]).name
        if base in DATABASE_CLIENT_COMMANDS and not self.runtime_policy.get("allow_shell_database_clients", False):
            return (
                False,
                f"Database client command '{base}' is blocked. Use PostgreSQL domain tools so SQL safety, approval, and audit controls apply.",
            )
        return True, None

    def invocation_environment_summary(self) -> dict[str, Any]:
        return {
            "workspace_root": self.workspace_profile["root_path"],
            "artifact_root": self.workspace_profile["artifact_root"],
            "task_workspace": {
                "task_id": (self.task_workspace or {}).get("task_id"),
                "root_path": (self.task_workspace or {}).get("root_path"),
            },
            "database_environment": self.database.safe_summary,
            "runtime_policy": {
                "allow_shell_database_clients": self.runtime_policy["allow_shell_database_clients"],
                "allow_database_writes": self.runtime_policy["allow_database_writes"],
                "require_approval_for_database_write": self.runtime_policy["require_approval_for_database_write"],
                "max_tool_duration_seconds": self.runtime_policy["max_tool_duration_seconds"],
            },
        }


def safe_connection_label(database_url: str | None) -> str | None:
    if not database_url:
        return database_url
    parsed = urlparse(database_url)
    if parsed.password is None:
        return database_url
    netloc = parsed.netloc.replace(f":{parsed.password}@", ":****@")
    return parsed._replace(netloc=netloc).geturl()
