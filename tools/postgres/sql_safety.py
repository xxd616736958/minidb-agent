"""PostgreSQL SQL classification and conservative safety checks."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, asdict
from typing import Any


WRITE_RE = re.compile(r"\b(insert|update|delete|merge|copy\s+.+\s+from)\b", re.IGNORECASE | re.DOTALL)
SCHEMA_RE = re.compile(r"\b(create|alter|drop|truncate)\b", re.IGNORECASE)
PERMISSION_RE = re.compile(r"\b(grant|revoke|alter\s+role|create\s+role|drop\s+role)\b", re.IGNORECASE)
MAINTENANCE_RE = re.compile(r"\b(vacuum|analyze|reindex|cluster|refresh\s+materialized\s+view)\b", re.IGNORECASE)
TRANSACTION_RE = re.compile(r"\b(begin|commit|rollback|savepoint|release\s+savepoint)\b", re.IGNORECASE)
LOCKING_RE = re.compile(r"\b(for\s+update|for\s+share|lock\s+table)\b", re.IGNORECASE)
DANGEROUS_FUNCTION_RE = re.compile(
    r"\b(pg_sleep|dblink|lo_import|lo_export|pg_read_file|pg_write_file|pg_ls_dir|copy\s+.+\s+program)\b",
    re.IGNORECASE | re.DOTALL,
)
EXPLAIN_ANALYZE_RE = re.compile(r"\bexplain\b[\s\S]*\banalyze\b", re.IGNORECASE)
NO_WHERE_MUTATION_RE = re.compile(r"\b(update|delete)\b(?![\s\S]*\bwhere\b)", re.IGNORECASE)


@dataclass
class SQLClassification:
    normalized_sql_hash: str
    statement_count: int
    primary_type: str
    risk_level: str
    read_only: bool
    destructive: bool
    requires_approval: bool
    requires_transaction: bool
    detected_operations: list[str]
    blocked_reasons: list[str]
    warnings: list[str]
    parser: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def normalize_sql(sql: str) -> str:
    return " ".join(sql.strip().split())


def sql_hash(sql: str) -> str:
    return hashlib.sha256(normalize_sql(sql).encode("utf-8")).hexdigest()[:16]


def _strip_comments(sql: str) -> str:
    sql = re.sub(r"--[^\n]*", " ", sql)
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    return sql


def _statement_count(sql: str) -> int:
    try:
        import pglast

        return len(pglast.parse_sql(sql))
    except Exception:
        parts = [part.strip() for part in _strip_comments(sql).split(";") if part.strip()]
        return len(parts)


def _leading_keyword(sql: str) -> str:
    sql = _strip_comments(sql).lstrip()
    match = re.match(r"([a-zA-Z_]+)", sql)
    return match.group(1).lower() if match else ""


def _ast_classify(sql: str) -> tuple[str, list[str], str] | None:
    """Return primary type, operations, parser name if pglast is available."""
    try:
        import pglast
    except Exception:
        return None

    try:
        parsed = pglast.parse_sql(sql)
    except Exception:
        return ("unknown", ["parse_error"], "pglast")

    operations: list[str] = []
    primary = "read_only"
    for stmt in parsed:
        node = getattr(stmt, "stmt", stmt)
        node_name = type(node).__name__.lower()
        operations.append(type(node).__name__)
        if "selectstmt" in node_name or "variable" in node_name:
            continue
        if "explainstmt" in node_name:
            primary = "explain"
            continue
        if any(token in node_name for token in ("insert", "update", "delete", "merge", "copystmt")):
            return ("data_change", operations, "pglast")
        if any(token in node_name for token in ("creat", "alter", "drop", "truncate")):
            return ("schema_change", operations, "pglast")
        if any(token in node_name for token in ("grant", "role", "revoke")):
            return ("permission_change", operations, "pglast")
        if any(token in node_name for token in ("vacuum", "reindex", "cluster")):
            return ("maintenance", operations, "pglast")
        primary = "unknown"
    return (primary, operations, "pglast")


def classify_sql(sql: str, *, allow_explain_analyze: bool = False) -> SQLClassification:
    """Classify SQL risk using AST when available, with conservative fallback."""
    normalized = normalize_sql(sql)
    operations: list[str] = []
    blocked: list[str] = []
    warnings: list[str] = []
    parser = "fallback"
    statement_count = _statement_count(sql)

    ast_result = _ast_classify(sql)
    if ast_result:
        primary_type, operations, parser = ast_result
    else:
        keyword = _leading_keyword(sql)
        if keyword in {"select", "show", "with"}:
            primary_type = "read_only"
        elif keyword == "explain":
            primary_type = "explain"
        elif WRITE_RE.search(sql):
            primary_type = "data_change"
        elif SCHEMA_RE.search(sql):
            primary_type = "schema_change"
        elif PERMISSION_RE.search(sql):
            primary_type = "permission_change"
        elif MAINTENANCE_RE.search(sql):
            primary_type = "maintenance"
        elif TRANSACTION_RE.search(sql):
            primary_type = "transaction_control"
        else:
            primary_type = "unknown"
        operations = [keyword or "unknown"]

    if statement_count > 1:
        warnings.append("multiple SQL statements detected")
    if DANGEROUS_FUNCTION_RE.search(sql):
        blocked.append("dangerous PostgreSQL function or COPY PROGRAM detected")
    if LOCKING_RE.search(sql):
        blocked.append("locking clause is not allowed in read-only tools")
    if EXPLAIN_ANALYZE_RE.search(sql) and not allow_explain_analyze:
        blocked.append("EXPLAIN ANALYZE requires explicit approval")
    if NO_WHERE_MUTATION_RE.search(sql):
        warnings.append("UPDATE or DELETE without WHERE detected")

    if WRITE_RE.search(sql):
        primary_type = "data_change"
    elif SCHEMA_RE.search(sql) and primary_type not in {"data_change"}:
        primary_type = "schema_change"
    elif PERMISSION_RE.search(sql):
        primary_type = "permission_change"
    elif MAINTENANCE_RE.search(sql) and primary_type == "unknown":
        primary_type = "maintenance"
    elif TRANSACTION_RE.search(sql) and primary_type == "unknown":
        primary_type = "transaction_control"

    read_only = primary_type in {"read_only", "explain", "diagnostic"} and not blocked
    destructive = primary_type in {
        "data_change",
        "schema_change",
        "permission_change",
        "maintenance",
        "transaction_control",
    }

    risk_level = "low"
    if primary_type == "explain":
        risk_level = "medium" if EXPLAIN_ANALYZE_RE.search(sql) else "low"
    if primary_type in {"maintenance", "schema_change"}:
        risk_level = "high"
    if primary_type in {"data_change", "permission_change"}:
        risk_level = "high"
    if "UPDATE or DELETE without WHERE detected" in warnings:
        risk_level = "critical"
    if blocked:
        risk_level = "high" if risk_level in {"low", "medium"} else risk_level

    return SQLClassification(
        normalized_sql_hash=sql_hash(sql),
        statement_count=statement_count,
        primary_type=primary_type,
        risk_level=risk_level,
        read_only=read_only,
        destructive=destructive,
        requires_approval=destructive or bool(blocked) or risk_level in {"high", "critical"},
        requires_transaction=primary_type in {"data_change", "schema_change", "maintenance"},
        detected_operations=operations,
        blocked_reasons=blocked,
        warnings=warnings,
        parser=parser,
    )
