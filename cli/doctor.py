"""CLI diagnostics for server, PostgreSQL, workspace, and configuration."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from cli.config import CliRuntimeConfig, build_db_connection_card, mask_database_url


def _check(name: str, status: str, summary: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "name": name,
        "status": status,
        "summary": summary,
        "details": details or {},
    }


async def run_doctor(config: CliRuntimeConfig, *, check_server: bool = True, check_database: bool = True) -> dict[str, Any]:
    """Run local diagnostics without printing secrets."""

    checks: list[dict[str, Any]] = []
    if check_server:
        checks.append(await _check_server(config))
    checks.append(_check_database_config(config))
    if check_database:
        checks.append(_check_postgres_connection(config))
    checks.append(_check_workspace(config))
    checks.append(_check_artifact_workspace(config))
    checks.append(_check_approval_mode(config))
    status = "failed" if any(item["status"] == "failed" for item in checks) else "warning" if any(item["status"] == "warning" for item in checks) else "passed"
    return {
        "status": status,
        "checks": checks,
    }


async def _check_server(config: CliRuntimeConfig) -> dict[str, Any]:
    try:
        import httpx

        headers = {"X-API-Key": config.api_key} if config.api_key else {}
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{config.server_url}/health", headers=headers)
        if resp.status_code == 200:
            payload = resp.json()
            return _check("server_health", "passed", "Agent server is healthy.", {"url": config.server_url, "service": payload.get("service")})
        if resp.status_code == 401:
            return _check("server_health", "failed", "Agent server rejected the API key.", {"url": config.server_url, "status_code": 401})
        return _check("server_health", "warning", f"Agent server returned HTTP {resp.status_code}.", {"url": config.server_url, "status_code": resp.status_code})
    except Exception as exc:
        return _check("server_health", "failed", f"Cannot reach agent server: {exc}", {"url": config.server_url})


def _check_database_config(config: CliRuntimeConfig) -> dict[str, Any]:
    card = build_db_connection_card(config)
    if not config.database_url and not config.db_profile:
        return _check("database_config", "warning", "PostgreSQL target is not configured for the CLI.", {"profile": None})
    return _check(
        "database_config",
        "passed",
        "PostgreSQL target metadata is configured.",
        {
            "database_url": mask_database_url(config.database_url),
            "db_profile": config.db_profile,
            "fingerprint": card["fingerprint"],
            "target_environment": card["target_environment"],
        },
    )


def _check_postgres_connection(config: CliRuntimeConfig) -> dict[str, Any]:
    if not config.database_url:
        return _check("postgres_connection", "warning", "No database URL available for a direct CLI connection check.")
    try:
        import psycopg
        from psycopg.rows import dict_row

        with psycopg.connect(config.database_url, row_factory=dict_row, connect_timeout=5) as conn:
            with conn.cursor() as cur:
                cur.execute("BEGIN TRANSACTION READ ONLY")
                cur.execute("SELECT current_database() AS database, current_user AS user, version() AS version")
                row = dict(cur.fetchone() or {})
                conn.rollback()
        return _check(
            "postgres_connection",
            "passed",
            "PostgreSQL connection check succeeded.",
            {"database": row.get("database"), "user": row.get("user"), "version": str(row.get("version", ""))[:120]},
        )
    except Exception as exc:
        return _check("postgres_connection", "failed", f"PostgreSQL connection check failed: {exc}", {"database_url": mask_database_url(config.database_url)})


def _check_workspace(config: CliRuntimeConfig) -> dict[str, Any]:
    path = Path(config.workspace).expanduser()
    try:
        path.mkdir(parents=True, exist_ok=True)
        if not os.access(path, os.R_OK | os.W_OK):
            return _check("workspace", "failed", "Workspace is not readable and writable.", {"path": str(path)})
        return _check("workspace", "passed", "Workspace is readable and writable.", {"path": str(path.resolve())})
    except Exception as exc:
        return _check("workspace", "failed", f"Workspace check failed: {exc}", {"path": str(path)})


def _check_artifact_workspace(config: CliRuntimeConfig) -> dict[str, Any]:
    root = Path(config.workspace).expanduser() / ".mini_agent"
    artifact_root = root / "artifacts"
    report_root = root / "reports"
    try:
        artifact_root.mkdir(parents=True, exist_ok=True)
        report_root.mkdir(parents=True, exist_ok=True)
        writable = os.access(artifact_root, os.W_OK) and os.access(report_root, os.W_OK)
        return _check(
            "artifact_workspace",
            "passed" if writable else "failed",
            "Artifact directories are writable." if writable else "Artifact directories are not writable.",
            {"artifact_root": str(artifact_root.resolve()), "report_root": str(report_root.resolve())},
        )
    except Exception as exc:
        return _check("artifact_workspace", "failed", f"Artifact directory check failed: {exc}", {"root": str(root)})


def _check_approval_mode(config: CliRuntimeConfig) -> dict[str, Any]:
    if config.target_environment in {"prod", "production"} and config.approval_mode == "never":
        return _check("approval_mode", "failed", "approval-mode=never is not allowed for production-like targets.")
    if config.readonly:
        return _check("approval_mode", "passed", "CLI is running in readonly mode.", {"approval_mode": config.approval_mode})
    return _check("approval_mode", "passed", "Approval mode is configured.", {"approval_mode": config.approval_mode})


def doctor_exit_code(report: dict[str, Any]) -> int:
    return 1 if report.get("status") == "failed" else 0
