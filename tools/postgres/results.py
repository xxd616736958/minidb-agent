"""Structured result helpers for PostgreSQL tools."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Literal, Optional
from typing_extensions import TypedDict


POSTGRES_RESULT_MARKER = "__mini_agent_postgres_tool_result__"


class PostgreSQLToolResult(TypedDict):
    """Stable result contract returned by all PostgreSQL tools."""

    tool_name: str
    success: bool
    result_type: Literal[
        "connection_status",
        "sql_classification",
        "schema_summary",
        "object_detail",
        "query_result",
        "explain_plan",
        "top_queries",
        "health_report",
        "lock_report",
        "index_advice",
        "dry_run_report",
        "write_result",
        "maintenance_result",
        "sql_error",
        "tool_error",
        "policy_denied",
    ]
    summary: str
    payload: dict[str, Any]
    row_count: Optional[int]
    affected_rows: Optional[int]
    sqlstate: Optional[str]
    duration_ms: int
    truncated: bool
    sensitive_fields_masked: list[str]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def make_result(
    *,
    tool_name: str,
    success: bool,
    result_type: str,
    summary: str,
    payload: dict[str, Any] | None = None,
    row_count: int | None = None,
    affected_rows: int | None = None,
    sqlstate: str | None = None,
    duration_ms: int = 0,
    truncated: bool = False,
    sensitive_fields_masked: list[str] | None = None,
) -> PostgreSQLToolResult:
    result: PostgreSQLToolResult = {
        "tool_name": tool_name,
        "success": success,
        "result_type": result_type,  # type: ignore[typeddict-item]
        "summary": summary,
        "payload": payload or {},
        "row_count": row_count,
        "affected_rows": affected_rows,
        "sqlstate": sqlstate,
        "duration_ms": duration_ms,
        "truncated": truncated,
        "sensitive_fields_masked": sensitive_fields_masked or [],
    }
    return result


def dumps_result(result: PostgreSQLToolResult) -> str:
    """Serialize a structured result in a ToolMessage-friendly envelope."""
    return json.dumps(
        {
            POSTGRES_RESULT_MARKER: True,
            "result": result,
        },
        ensure_ascii=False,
        default=str,
        separators=(",", ":"),
    )


def loads_result(content: str) -> PostgreSQLToolResult | None:
    """Parse a structured PostgreSQL result from a tool message if present."""
    try:
        data = json.loads(content)
    except Exception:
        return None
    if not isinstance(data, dict) or not data.get(POSTGRES_RESULT_MARKER):
        return None
    result = data.get("result")
    if not isinstance(result, dict):
        return None
    return result  # type: ignore[return-value]
