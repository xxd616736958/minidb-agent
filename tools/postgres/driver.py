"""PostgreSQL connection and execution helpers."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any, Optional

from agent.config import get_settings
from execution.environment import build_database_environment_profile
from tools.postgres.sanitizer import limit_rows, obfuscate_password


@dataclass
class QueryResult:
    rows: list[dict[str, Any]]
    row_count: int | None
    affected_rows: int | None
    sqlstate: str | None
    duration_ms: int
    truncated: bool
    sensitive_fields_masked: list[str]


class PostgresConnectionManager:
    """Small sync connection manager for PostgreSQL tools."""

    def __init__(self, database_url: str | None = None) -> None:
        self.database_url = database_url or os.environ.get("POSTGRES_TARGET_URL") or get_settings().postgres_uri
        self.profile = build_database_environment_profile(self.database_url)

    def check_configured(self) -> None:
        if not self.database_url:
            raise ValueError("PostgreSQL connection string is not configured. Set POSTGRES_TARGET_URL or POSTGRES_URI.")

    @property
    def safe_database_url(self) -> str | None:
        return obfuscate_password(self.database_url)


class PostgresDriver:
    """Sync psycopg driver used by concrete PostgreSQL tools."""

    def __init__(self, manager: PostgresConnectionManager | None = None) -> None:
        self.manager = manager or PostgresConnectionManager()

    def execute(
        self,
        sql: str,
        *,
        params: list[Any] | tuple[Any, ...] | None = None,
        readonly: bool = False,
        max_rows: int = 100,
        statement_timeout_ms: int | None = None,
        lock_timeout_ms: int | None = None,
    ) -> QueryResult:
        """Execute SQL and return masked, limited rows."""
        self.manager.check_configured()
        max_rows = min(max_rows, int(self.manager.profile.get("max_result_rows", max_rows) or max_rows))
        statement_timeout_ms = statement_timeout_ms or int(self.manager.profile.get("default_statement_timeout_ms", 30_000))
        lock_timeout_ms = lock_timeout_ms or int(self.manager.profile.get("default_lock_timeout_ms", 5_000))
        try:
            import psycopg
            from psycopg.rows import dict_row
        except Exception as exc:
            raise RuntimeError("psycopg is required for PostgreSQL tools") from exc

        started = time.monotonic()
        try:
            with psycopg.connect(self.manager.database_url, row_factory=dict_row) as conn:
                with conn.cursor() as cur:
                    cur.execute("SET statement_timeout = %s", (statement_timeout_ms,))
                    cur.execute("SET lock_timeout = %s", (lock_timeout_ms,))
                    if readonly:
                        cur.execute("BEGIN TRANSACTION READ ONLY")
                    cur.execute(sql, params)
                    rows: list[dict[str, Any]] = []
                    if cur.description is not None:
                        rows = [dict(row) for row in cur.fetchall()]
                    affected = cur.rowcount if cur.description is None and cur.rowcount >= 0 else None
                    if readonly:
                        conn.rollback()
                    else:
                        conn.commit()
        except Exception as exc:
            sqlstate = getattr(exc, "sqlstate", None)
            message = obfuscate_password(str(exc))
            err = RuntimeError(message or "PostgreSQL execution failed")
            setattr(err, "sqlstate", sqlstate)
            raise err from exc

        duration_ms = int((time.monotonic() - started) * 1000)
        limited_rows, truncated, masked_fields = limit_rows(rows, max_rows=max_rows)
        return QueryResult(
            rows=limited_rows,
            row_count=len(rows) if rows else 0,
            affected_rows=affected,
            sqlstate=None,
            duration_ms=duration_ms,
            truncated=truncated,
            sensitive_fields_masked=masked_fields,
        )

    def connection_check(self) -> QueryResult:
        return self.execute("SELECT current_database() AS database, current_user AS user, version() AS version", readonly=True, max_rows=1)
