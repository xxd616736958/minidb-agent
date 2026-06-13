"""PostgreSQL domain tools for database management tasks."""

from __future__ import annotations

import re
import time
from typing import Any, Literal, Optional, Type

from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from execution.environment import build_database_environment_profile
from tools.base import AgentTool
from tools.postgres.driver import PostgresDriver
from tools.postgres.results import dumps_result, make_result
from tools.postgres.sanitizer import limit_payload, obfuscate_password
from tools.postgres.sql_safety import classify_sql


def _driver() -> PostgresDriver:
    return PostgresDriver()


def _write_blocked_by_environment() -> str | None:
    profile = build_database_environment_profile()
    if not profile.get("allow_write_tools"):
        return (
            "PostgreSQL write-capable tools are disabled for "
            f"target environment '{profile.get('environment_name')}'."
        )
    return None


def _error_result(tool_name: str, result_type: str, error: Exception, duration_ms: int = 0) -> str:
    sqlstate = getattr(error, "sqlstate", None)
    summary = obfuscate_password(str(error)) or "PostgreSQL tool failed"
    return dumps_result(
        make_result(
            tool_name=tool_name,
            success=False,
            result_type=result_type,
            summary=summary,
            payload={"error": summary},
            sqlstate=sqlstate,
            duration_ms=duration_ms,
            sensitive_fields_masked=["error"] if summary != str(error) else [],
        )
    )


def _timed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _quote_ident(identifier: str) -> str:
    if not IDENTIFIER_RE.match(identifier):
        raise ValueError(f"Unsafe PostgreSQL identifier: {identifier}")
    return f'"{identifier}"'


def _quote_qualified_name(name: str) -> str:
    parts = [part for part in name.split(".") if part]
    if not parts:
        raise ValueError("PostgreSQL identifier cannot be empty")
    return ".".join(_quote_ident(part.strip('"')) for part in parts)


class ConnectionCheckInput(BaseModel):
    include_version: bool = Field(default=True, description="Whether to include the PostgreSQL server version.")


class PostgresConnectionCheckTool(AgentTool):
    """Check PostgreSQL connectivity through the configured connection string."""

    name: str = "postgres_connection_check"
    description: str = "Check whether the configured PostgreSQL target can be reached and return safe connection metadata."
    args_schema: Type[BaseModel] = ConnectionCheckInput
    tool_domain: str = "postgresql"
    operation_type: str = "diagnostic"
    risk_level: str = "low"
    read_only: bool = True
    destructive: bool = False
    requires_approval: bool = False
    allowed_phases: list[str] = ["observe", "diagnose", "verify"]
    allowed_policies: list[str] = ["read_only_tools", "write_tools_after_approval"]
    output_type: str = "connection_status"
    result_sensitivity: str = "internal"
    search_hint: str | None = "check PostgreSQL database connectivity"

    def _run(self, include_version: bool = True) -> str:
        started = time.monotonic()
        try:
            result = _driver().connection_check()
            payload = result.rows[0] if result.rows else {}
            if not include_version:
                payload.pop("version", None)
            return dumps_result(
                make_result(
                    tool_name=self.name,
                    success=True,
                    result_type="connection_status",
                    summary="PostgreSQL connection is available.",
                    payload=payload,
                    row_count=result.row_count,
                    duration_ms=result.duration_ms,
                    truncated=result.truncated,
                    sensitive_fields_masked=result.sensitive_fields_masked,
                )
            )
        except Exception as exc:
            return _error_result(self.name, "tool_error", exc, _timed_ms(started))


class SQLClassifyInput(BaseModel):
    sql: str = Field(description="SQL text to classify before execution.", min_length=1, max_length=50_000)
    allow_explain_analyze: bool = Field(default=False, description="Whether EXPLAIN ANALYZE is explicitly allowed.")


class PostgresSQLClassifyTool(AgentTool):
    """Classify SQL operation type, risk, and approval needs."""

    name: str = "postgres_sql_classify"
    description: str = "Classify PostgreSQL SQL by operation type, risk level, read-only status, and blocked reasons without executing it."
    args_schema: Type[BaseModel] = SQLClassifyInput
    tool_domain: str = "postgresql"
    operation_type: str = "diagnostic"
    risk_level: str = "low"
    read_only: bool = True
    destructive: bool = False
    requires_approval: bool = False
    allowed_phases: list[str] = ["observe", "diagnose", "propose", "approve", "verify"]
    allowed_policies: list[str] = ["read_only_tools", "write_tools_after_approval"]
    output_type: str = "sql_classification"
    result_sensitivity: str = "internal"
    search_hint: str | None = "classify SQL safety and risk"

    def _run(self, sql: str, allow_explain_analyze: bool = False) -> str:
        started = time.monotonic()
        classification = classify_sql(sql, allow_explain_analyze=allow_explain_analyze)
        return dumps_result(
            make_result(
                tool_name=self.name,
                success=True,
                result_type="sql_classification",
                summary=f"SQL classified as {classification.primary_type} with {classification.risk_level} risk.",
                payload={
                    **classification.to_dict(),
                    "sql": sql,
                    "sql_hash": classification.normalized_sql_hash,
                },
                duration_ms=_timed_ms(started),
            )
        )


class ListDatabasesInput(BaseModel):
    include_templates: bool = Field(default=False, description="Whether to include template databases.")


class PostgresListDatabasesTool(AgentTool):
    name: str = "postgres_list_databases"
    description: str = "List visible PostgreSQL databases with owner, encoding, collation, connection allowance, and size."
    args_schema: Type[BaseModel] = ListDatabasesInput
    tool_domain: str = "postgresql"
    operation_type: str = "read_only"
    risk_level: str = "low"
    read_only: bool = True
    destructive: bool = False
    requires_approval: bool = False
    allowed_phases: list[str] = ["observe", "diagnose", "verify", "report"]
    allowed_policies: list[str] = ["read_only_tools", "write_tools_after_approval"]
    output_type: str = "database_list"
    result_sensitivity: str = "internal"
    search_hint: str | None = "list PostgreSQL databases current cluster"

    def _run(self, include_templates: bool = False) -> str:
        started = time.monotonic()
        sql = """
            SELECT d.datname AS database,
                   pg_catalog.pg_get_userbyid(d.datdba) AS owner,
                   pg_catalog.pg_encoding_to_char(d.encoding) AS encoding,
                   d.datcollate AS collation,
                   d.datallowconn AS allow_connections,
                   pg_catalog.pg_size_pretty(pg_catalog.pg_database_size(d.datname)) AS size
            FROM pg_catalog.pg_database d
            WHERE %s OR NOT d.datistemplate
            ORDER BY d.datistemplate, d.datname
        """
        try:
            result = _driver().execute(sql, params=[include_templates], readonly=True, max_rows=200)
            return dumps_result(
                make_result(
                    tool_name=self.name,
                    success=True,
                    result_type="database_list",
                    summary=f"Found {result.row_count} database(s).",
                    payload={"databases": result.rows, "include_templates": include_templates},
                    row_count=result.row_count,
                    duration_ms=result.duration_ms,
                    truncated=result.truncated,
                    sensitive_fields_masked=result.sensitive_fields_masked,
                )
            )
        except Exception as exc:
            return _error_result(self.name, "sql_error", exc, _timed_ms(started))


class ListSchemasInput(BaseModel):
    include_system: bool = Field(default=False, description="Whether to include pg_* and information_schema schemas.")


class PostgresListSchemasTool(AgentTool):
    name: str = "postgres_list_schemas"
    description: str = "List PostgreSQL schemas with owner and schema type."
    args_schema: Type[BaseModel] = ListSchemasInput
    tool_domain: str = "postgresql"
    operation_type: str = "read_only"
    risk_level: str = "low"
    read_only: bool = True
    destructive: bool = False
    allowed_phases: list[str] = ["observe", "diagnose", "verify", "report"]
    allowed_policies: list[str] = ["read_only_tools", "write_tools_after_approval"]
    output_type: str = "schema_summary"
    result_sensitivity: str = "internal"
    search_hint: str | None = "list PostgreSQL schemas"

    def _run(self, include_system: bool = False) -> str:
        started = time.monotonic()
        sql = """
            SELECT
                schema_name,
                schema_owner,
                CASE
                    WHEN starts_with(schema_name, 'pg_') THEN 'system'
                    WHEN schema_name = 'information_schema' THEN 'system'
                    ELSE 'user'
                END AS schema_type
            FROM information_schema.schemata
            WHERE %s OR (NOT starts_with(schema_name, 'pg_') AND schema_name <> 'information_schema')
            ORDER BY schema_type, schema_name
        """
        try:
            result = _driver().execute(sql, params=[include_system], readonly=True, max_rows=300)
            payload = {"schemas": result.rows}
            return dumps_result(
                make_result(
                    tool_name=self.name,
                    success=True,
                    result_type="schema_summary",
                    summary=f"Found {result.row_count} schema(s).",
                    payload=payload,
                    row_count=result.row_count,
                    duration_ms=result.duration_ms,
                    truncated=result.truncated,
                    sensitive_fields_masked=result.sensitive_fields_masked,
                )
            )
        except Exception as exc:
            return _error_result(self.name, "sql_error", exc, _timed_ms(started))


class SchemaOverviewInput(BaseModel):
    include_system: bool = Field(default=False, description="Whether to include system schemas.")
    table_limit: int = Field(default=500, ge=1, le=1000, description="Maximum user tables to return.")


class PostgresSchemaOverviewTool(AgentTool):
    name: str = "postgres_schema_overview"
    description: str = "Return current PostgreSQL connection metadata, schemas, and visible tables in one read-only call."
    args_schema: Type[BaseModel] = SchemaOverviewInput
    tool_domain: str = "postgresql"
    operation_type: str = "read_only"
    risk_level: str = "low"
    read_only: bool = True
    destructive: bool = False
    allowed_phases: list[str] = ["observe", "diagnose", "verify", "report"]
    allowed_policies: list[str] = ["read_only_tools", "write_tools_after_approval"]
    output_type: str = "schema_summary"
    result_sensitivity: str = "internal"
    search_hint: str | None = "overview PostgreSQL current database schemas tables connection"

    def _run(self, include_system: bool = False, table_limit: int = 500) -> str:
        started = time.monotonic()
        schema_filter = "%s OR (NOT starts_with(schema_name, 'pg_') AND schema_name <> 'information_schema')"
        table_filter = "%s OR (NOT starts_with(t.table_schema, 'pg_') AND t.table_schema <> 'information_schema')"
        try:
            drv = _driver()
            connection = drv.execute(
                "SELECT current_database() AS database, current_user AS user, inet_server_addr()::text AS host, inet_server_port() AS port, version() AS version",
                readonly=True,
                max_rows=1,
            )
            schemas = drv.execute(
                f"""
                SELECT schema_name,
                       schema_owner,
                       CASE
                           WHEN starts_with(schema_name, 'pg_') THEN 'system'
                           WHEN schema_name = 'information_schema' THEN 'system'
                           ELSE 'user'
                       END AS schema_type
                FROM information_schema.schemata
                WHERE {schema_filter}
                ORDER BY schema_type, schema_name
                """,
                params=[include_system],
                readonly=True,
                max_rows=300,
            )
            tables = drv.execute(
                f"""
                SELECT t.table_schema AS schema,
                       t.table_name AS name,
                       t.table_type AS type
                FROM information_schema.tables t
                WHERE {table_filter}
                  AND t.table_type IN ('BASE TABLE', 'VIEW')
                ORDER BY t.table_schema, t.table_name
                LIMIT %s
                """,
                params=[include_system, table_limit],
                readonly=True,
                max_rows=table_limit,
            )
            payload = {
                "connection": connection.rows[0] if connection.rows else {},
                "schemas": schemas.rows,
                "tables": tables.rows,
                "include_system": include_system,
            }
            safe_payload, payload_truncated, payload_masked = limit_payload(payload)
            duration = connection.duration_ms + schemas.duration_ms + tables.duration_ms
            return dumps_result(
                make_result(
                    tool_name=self.name,
                    success=True,
                    result_type="schema_summary",
                    summary=f"Collected target overview: {schemas.row_count} schema(s), {tables.row_count} table/view object(s).",
                    payload=safe_payload,
                    row_count=(schemas.row_count or 0) + (tables.row_count or 0),
                    duration_ms=duration or _timed_ms(started),
                    truncated=connection.truncated or schemas.truncated or tables.truncated or payload_truncated,
                    sensitive_fields_masked=[
                        *connection.sensitive_fields_masked,
                        *schemas.sensitive_fields_masked,
                        *tables.sensitive_fields_masked,
                        *payload_masked,
                    ],
                )
            )
        except Exception as exc:
            return _error_result(self.name, "sql_error", exc, _timed_ms(started))


class ListObjectsInput(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    schema_name: str = Field(
        description="Schema name to inspect.",
        min_length=1,
        max_length=256,
        validation_alias=AliasChoices("schema_name", "schema"),
    )
    object_type: Literal["table", "view", "sequence", "extension"] = Field(default="table", description="Object type to list.")
    limit: int = Field(default=200, ge=1, le=1000, description="Maximum objects to return.")


class PostgresListObjectsTool(AgentTool):
    name: str = "postgres_list_objects"
    description: str = "List tables, views, sequences, or extensions in PostgreSQL."
    args_schema: Type[BaseModel] = ListObjectsInput
    tool_domain: str = "postgresql"
    operation_type: str = "read_only"
    risk_level: str = "low"
    read_only: bool = True
    destructive: bool = False
    allowed_phases: list[str] = ["observe", "diagnose", "verify", "report"]
    allowed_policies: list[str] = ["read_only_tools", "write_tools_after_approval"]
    output_type: str = "schema_summary"
    result_sensitivity: str = "internal"
    search_hint: str | None = "list PostgreSQL tables views sequences extensions"

    def _run(self, schema_name: str, object_type: str = "table", limit: int = 200) -> str:
        started = time.monotonic()
        try:
            if object_type in {"table", "view"}:
                table_type = "BASE TABLE" if object_type == "table" else "VIEW"
                sql = """
                    SELECT table_schema AS schema, table_name AS name, table_type AS type
                    FROM information_schema.tables
                    WHERE table_schema = %s AND table_type = %s
                    ORDER BY table_name
                    LIMIT %s
                """
                params: list[Any] = [schema_name, table_type, limit]
            elif object_type == "sequence":
                sql = """
                    SELECT sequence_schema AS schema, sequence_name AS name, data_type
                    FROM information_schema.sequences
                    WHERE sequence_schema = %s
                    ORDER BY sequence_name
                    LIMIT %s
                """
                params = [schema_name, limit]
            else:
                sql = """
                    SELECT extname AS name, extversion AS version, extrelocatable AS relocatable
                    FROM pg_extension
                    ORDER BY extname
                    LIMIT %s
                """
                params = [limit]
            result = _driver().execute(sql, params=params, readonly=True, max_rows=limit)
            return dumps_result(
                make_result(
                    tool_name=self.name,
                    success=True,
                    result_type="schema_summary",
                    summary=f"Found {result.row_count} {object_type} object(s).",
                    payload={"schema": schema_name, "object_type": object_type, "objects": result.rows},
                    row_count=result.row_count,
                    duration_ms=result.duration_ms,
                    truncated=result.truncated,
                    sensitive_fields_masked=result.sensitive_fields_masked,
                )
            )
        except Exception as exc:
            return _error_result(self.name, "sql_error", exc, _timed_ms(started))


class ObjectDetailInput(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    schema_name: str = Field(
        description="Schema name.",
        min_length=1,
        max_length=256,
        validation_alias=AliasChoices("schema_name", "schema"),
    )
    object_name: str = Field(
        description="Object name.",
        min_length=1,
        max_length=256,
        validation_alias=AliasChoices("object_name", "name", "table"),
    )
    object_type: Literal["table", "view", "sequence", "extension"] = Field(default="table", description="Object type.")


class PostgresObjectDetailTool(AgentTool):
    name: str = "postgres_object_detail"
    description: str = "Show PostgreSQL object details including columns, constraints, and indexes."
    args_schema: Type[BaseModel] = ObjectDetailInput
    tool_domain: str = "postgresql"
    operation_type: str = "read_only"
    risk_level: str = "low"
    read_only: bool = True
    destructive: bool = False
    allowed_phases: list[str] = ["observe", "diagnose", "verify", "report"]
    allowed_policies: list[str] = ["read_only_tools", "write_tools_after_approval"]
    output_type: str = "object_detail"
    result_sensitivity: str = "internal"
    search_hint: str | None = "inspect PostgreSQL table columns constraints indexes"

    def _run(self, schema_name: str, object_name: str, object_type: str = "table") -> str:
        started = time.monotonic()
        try:
            drv = _driver()
            if object_type in {"table", "view"}:
                col_sql = """
                    SELECT column_name, data_type, is_nullable, column_default
                    FROM information_schema.columns
                    WHERE table_schema = %s AND table_name = %s
                    ORDER BY ordinal_position
                """
                con_sql = """
                    SELECT tc.constraint_name, tc.constraint_type, kcu.column_name
                    FROM information_schema.table_constraints tc
                    LEFT JOIN information_schema.key_column_usage kcu
                      ON tc.constraint_name = kcu.constraint_name
                     AND tc.table_schema = kcu.table_schema
                    WHERE tc.table_schema = %s AND tc.table_name = %s
                    ORDER BY tc.constraint_name, kcu.ordinal_position
                """
                idx_sql = """
                    SELECT indexname AS name, indexdef AS definition
                    FROM pg_indexes
                    WHERE schemaname = %s AND tablename = %s
                    ORDER BY indexname
                """
                cols = drv.execute(col_sql, params=[schema_name, object_name], readonly=True, max_rows=500)
                cons = drv.execute(con_sql, params=[schema_name, object_name], readonly=True, max_rows=500)
                idxs = drv.execute(idx_sql, params=[schema_name, object_name], readonly=True, max_rows=500)
                payload = {
                    "basic": {"schema": schema_name, "name": object_name, "type": object_type},
                    "columns": cols.rows,
                    "constraints": cons.rows,
                    "indexes": idxs.rows,
                }
                duration = cols.duration_ms + cons.duration_ms + idxs.duration_ms
                row_count = len(cols.rows) + len(cons.rows) + len(idxs.rows)
                truncated = cols.truncated or cons.truncated or idxs.truncated
                masked = [*cols.sensitive_fields_masked, *cons.sensitive_fields_masked, *idxs.sensitive_fields_masked]
            elif object_type == "sequence":
                seq = drv.execute(
                    """
                    SELECT sequence_schema, sequence_name, data_type, start_value, increment
                    FROM information_schema.sequences
                    WHERE sequence_schema = %s AND sequence_name = %s
                    """,
                    params=[schema_name, object_name],
                    readonly=True,
                    max_rows=1,
                )
                payload = {"sequence": seq.rows[0] if seq.rows else None}
                duration = seq.duration_ms
                row_count = seq.row_count
                truncated = seq.truncated
                masked = seq.sensitive_fields_masked
            else:
                ext = drv.execute(
                    """
                    SELECT extname AS name, extversion AS version, extrelocatable AS relocatable
                    FROM pg_extension
                    WHERE extname = %s
                    """,
                    params=[object_name],
                    readonly=True,
                    max_rows=1,
                )
                payload = {"extension": ext.rows[0] if ext.rows else None}
                duration = ext.duration_ms
                row_count = ext.row_count
                truncated = ext.truncated
                masked = ext.sensitive_fields_masked
            safe_payload, payload_truncated, payload_masked = limit_payload(payload)
            return dumps_result(
                make_result(
                    tool_name=self.name,
                    success=True,
                    result_type="object_detail",
                    summary=f"Collected details for {schema_name}.{object_name}.",
                    payload=safe_payload,
                    row_count=row_count,
                    duration_ms=duration,
                    truncated=truncated or payload_truncated,
                    sensitive_fields_masked=[*masked, *payload_masked],
                )
            )
        except Exception as exc:
            return _error_result(self.name, "sql_error", exc, _timed_ms(started))


class ReadonlyQueryInput(BaseModel):
    sql: str = Field(description="Read-only SQL query to execute.", min_length=1, max_length=50_000)
    max_rows: int = Field(default=100, ge=1, le=1000, description="Maximum result rows to return.")


class PostgresQueryReadonlyTool(AgentTool):
    name: str = "postgres_query_readonly"
    description: str = "Execute a strictly read-only PostgreSQL query after SQL safety classification and read-only transaction enforcement."
    args_schema: Type[BaseModel] = ReadonlyQueryInput
    tool_domain: str = "postgresql"
    operation_type: str = "read_only"
    risk_level: str = "low"
    read_only: bool = True
    destructive: bool = False
    allowed_phases: list[str] = ["observe", "diagnose", "verify", "report"]
    allowed_policies: list[str] = ["read_only_tools", "write_tools_after_approval"]
    output_type: str = "query_result"
    result_sensitivity: str = "sensitive"
    search_hint: str | None = "execute safe read-only PostgreSQL SELECT SHOW WITH query"

    def _run(self, sql: str, max_rows: int = 100) -> str:
        started = time.monotonic()
        classification = classify_sql(sql)
        if not classification.read_only or classification.primary_type not in {"read_only", "diagnostic"}:
            return dumps_result(
                make_result(
                    tool_name=self.name,
                    success=False,
                    result_type="policy_denied",
                    summary=f"Read-only query rejected: {classification.primary_type}.",
                    payload={"classification": classification.to_dict()},
                    duration_ms=_timed_ms(started),
                )
            )
        if classification.statement_count > 1:
            return dumps_result(
                make_result(
                    tool_name=self.name,
                    success=False,
                    result_type="policy_denied",
                    summary="Read-only query rejected: multiple statements are not allowed.",
                    payload={"classification": classification.to_dict()},
                    duration_ms=_timed_ms(started),
                )
            )
        try:
            result = _driver().execute(sql, readonly=True, max_rows=max_rows)
            return dumps_result(
                make_result(
                    tool_name=self.name,
                    success=True,
                    result_type="query_result",
                    summary=f"Read-only query returned {result.row_count} row(s).",
                    payload={"classification": classification.to_dict(), "rows": result.rows},
                    row_count=result.row_count,
                    duration_ms=result.duration_ms,
                    truncated=result.truncated,
                    sensitive_fields_masked=result.sensitive_fields_masked,
                )
            )
        except Exception as exc:
            return _error_result(self.name, "sql_error", exc, _timed_ms(started))


class ExplainInput(BaseModel):
    sql: str = Field(description="SQL query to explain.", min_length=1, max_length=50_000)
    analyze: bool = Field(default=False, description="Whether to run EXPLAIN ANALYZE. Requires explicit approval in production workflows.")


def _summarize_plan(plan_data: Any) -> dict[str, Any]:
    if isinstance(plan_data, list) and plan_data:
        plan_data = plan_data[0]
    if not isinstance(plan_data, dict):
        return {"raw_plan": plan_data}
    root = plan_data.get("Plan", plan_data)
    scan_types: list[str] = []
    join_types: list[str] = []
    relations: list[str] = []

    def walk(node: dict[str, Any]) -> None:
        node_type = str(node.get("Node Type", ""))
        if "Scan" in node_type:
            scan_types.append(node_type)
        if "Join" in node_type:
            join_types.append(node_type)
        if node.get("Relation Name"):
            relations.append(str(node["Relation Name"]))
        for child in node.get("Plans", []) or []:
            if isinstance(child, dict):
                walk(child)

    if isinstance(root, dict):
        walk(root)
    return {
        "root_node_type": root.get("Node Type") if isinstance(root, dict) else None,
        "total_cost": root.get("Total Cost") if isinstance(root, dict) else None,
        "plan_rows": root.get("Plan Rows") if isinstance(root, dict) else None,
        "planning_time_ms": plan_data.get("Planning Time"),
        "execution_time_ms": plan_data.get("Execution Time"),
        "scan_types": scan_types,
        "join_types": join_types,
        "relation_names": sorted(set(relations)),
        "has_seq_scan": any(scan == "Seq Scan" for scan in scan_types),
        "has_index_scan": any("Index" in scan for scan in scan_types),
    }


class PostgresExplainTool(AgentTool):
    name: str = "postgres_explain"
    description: str = "Run EXPLAIN (FORMAT JSON) for a PostgreSQL query and return a structured execution-plan summary."
    args_schema: Type[BaseModel] = ExplainInput
    tool_domain: str = "postgresql"
    operation_type: str = "diagnostic"
    risk_level: str = "medium"
    read_only: bool = True
    destructive: bool = False
    requires_approval: bool = False
    allowed_phases: list[str] = ["observe", "diagnose", "verify"]
    allowed_policies: list[str] = ["read_only_tools", "write_tools_after_approval"]
    output_type: str = "explain_plan"
    result_sensitivity: str = "internal"
    search_hint: str | None = "explain PostgreSQL query plan"

    def _run(self, sql: str, analyze: bool = False) -> str:
        started = time.monotonic()
        classification = classify_sql(sql, allow_explain_analyze=analyze)
        requested_analyze = bool(analyze)
        if analyze:
            analyze = False
        if classification.primary_type not in {"read_only", "diagnostic"}:
            return dumps_result(
                make_result(
                    tool_name=self.name,
                    success=False,
                    result_type="policy_denied",
                    summary=f"EXPLAIN rejected for non-read SQL: {classification.primary_type}.",
                    payload={"classification": classification.to_dict()},
                    duration_ms=_timed_ms(started),
                )
            )
        try:
            result = _driver().execute(f"EXPLAIN (FORMAT JSON) {sql}", readonly=True, max_rows=1)
            raw_plan = result.rows[0].get("QUERY PLAN") if result.rows else None
            plan_summary = _summarize_plan(raw_plan)
            summary = f"Plan root: {plan_summary.get('root_node_type', 'unknown')}, cost={plan_summary.get('total_cost')}."
            if requested_analyze:
                summary += " EXPLAIN ANALYZE was downgraded to safe EXPLAIN without execution."
            return dumps_result(
                make_result(
                    tool_name=self.name,
                    success=True,
                    result_type="explain_plan",
                    summary=summary,
                    payload={
                        "classification": classification.to_dict(),
                        "plan": plan_summary,
                        "raw_plan": raw_plan,
                        "analyze_requested": requested_analyze,
                        "analyze_executed": False,
                    },
                    row_count=result.row_count,
                    duration_ms=result.duration_ms,
                    truncated=result.truncated,
                    sensitive_fields_masked=result.sensitive_fields_masked,
                )
            )
        except Exception as exc:
            return _error_result(self.name, "sql_error", exc, _timed_ms(started))


class TopQueriesInput(BaseModel):
    sort_by: Literal["resources", "mean_time", "total_time"] = Field(default="resources", description="Ranking criteria.")
    limit: int = Field(default=10, ge=1, le=50, description="Number of queries to return.")


class PostgresTopQueriesTool(AgentTool):
    name: str = "postgres_top_queries"
    description: str = "Report slow or resource-intensive queries from pg_stat_statements."
    args_schema: Type[BaseModel] = TopQueriesInput
    tool_domain: str = "postgresql"
    operation_type: str = "diagnostic"
    risk_level: str = "low"
    read_only: bool = True
    destructive: bool = False
    allowed_phases: list[str] = ["observe", "diagnose", "verify", "report"]
    allowed_policies: list[str] = ["read_only_tools", "write_tools_after_approval"]
    output_type: str = "top_queries"
    result_sensitivity: str = "sensitive"
    search_hint: str | None = "find top slow resource intensive PostgreSQL queries"

    def _run(self, sort_by: str = "resources", limit: int = 10) -> str:
        started = time.monotonic()
        order_expr = {
            "resources": "(shared_blks_read + shared_blks_hit + temp_blks_read + temp_blks_written) DESC",
            "mean_time": "mean_exec_time DESC",
            "total_time": "total_exec_time DESC",
        }.get(sort_by, "total_exec_time DESC")
        sql = f"""
            SELECT queryid, calls, rows,
                   total_exec_time, mean_exec_time,
                   shared_blks_hit, shared_blks_read, temp_blks_read, temp_blks_written,
                   left(regexp_replace(query, '\\s+', ' ', 'g'), 500) AS query_preview
            FROM pg_stat_statements
            ORDER BY {order_expr}
            LIMIT %s
        """
        try:
            drv = _driver()
            extension = drv.execute(
                "SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_stat_statements') AS installed",
                readonly=True,
                max_rows=1,
            )
            installed = bool(extension.rows and extension.rows[0].get("installed"))
            if not installed:
                activity = drv.execute(
                    """
                    SELECT pid, usename, state,
                           now() - query_start AS running_for,
                           wait_event_type,
                           wait_event,
                           left(regexp_replace(query, '\\s+', ' ', 'g'), 500) AS query_preview
                    FROM pg_stat_activity
                    WHERE query_start IS NOT NULL
                      AND state <> 'idle'
                      AND pid <> pg_backend_pid()
                    ORDER BY query_start ASC
                    LIMIT %s
                    """,
                    params=[limit],
                    readonly=True,
                    max_rows=limit,
                )
                return dumps_result(
                    make_result(
                        tool_name=self.name,
                        success=True,
                        result_type="top_queries",
                        summary="pg_stat_statements is not installed; returned current active queries only.",
                        payload={
                            "sort_by": sort_by,
                            "history_available": False,
                            "limitation": "Historical slow query statistics require the pg_stat_statements extension.",
                            "active_queries": activity.rows,
                        },
                        row_count=activity.row_count,
                        duration_ms=extension.duration_ms + activity.duration_ms,
                        truncated=activity.truncated,
                        sensitive_fields_masked=activity.sensitive_fields_masked,
                    )
                )
            result = drv.execute(sql, params=[limit], readonly=True, max_rows=limit)
            return dumps_result(
                make_result(
                    tool_name=self.name,
                    success=True,
                    result_type="top_queries",
                    summary=f"Collected {result.row_count} top query row(s) ordered by {sort_by}.",
                    payload={"sort_by": sort_by, "history_available": True, "queries": result.rows},
                    row_count=result.row_count,
                    duration_ms=result.duration_ms,
                    truncated=result.truncated,
                    sensitive_fields_masked=result.sensitive_fields_masked,
                )
            )
        except Exception as exc:
            return _error_result(self.name, "sql_error", exc, _timed_ms(started))


class HealthInput(BaseModel):
    health_type: Literal["index", "connection", "vacuum", "sequence", "replication", "buffer", "constraint", "all"] = Field(
        default="all",
        description="Health check dimension.",
    )


class PostgresHealthCheckTool(AgentTool):
    name: str = "postgres_health_check"
    description: str = "Run read-only PostgreSQL health checks for indexes, connections, vacuum, sequences, replication, buffers, and constraints."
    args_schema: Type[BaseModel] = HealthInput
    tool_domain: str = "postgresql"
    operation_type: str = "diagnostic"
    risk_level: str = "low"
    read_only: bool = True
    destructive: bool = False
    allowed_phases: list[str] = ["observe", "diagnose", "verify", "report"]
    allowed_policies: list[str] = ["read_only_tools", "write_tools_after_approval"]
    output_type: str = "health_report"
    result_sensitivity: str = "internal"
    search_hint: str | None = "check PostgreSQL database health"

    _CHECKS: dict[str, str] = {
        "connection": """
            SELECT count(*) AS total_connections,
                   count(*) FILTER (WHERE state = 'active') AS active_connections,
                   count(*) FILTER (WHERE state = 'idle in transaction') AS idle_in_transaction
            FROM pg_stat_activity
        """,
        "buffer": """
            SELECT datname, blks_hit, blks_read,
                   CASE WHEN blks_hit + blks_read = 0 THEN NULL
                        ELSE round(100.0 * blks_hit / (blks_hit + blks_read), 2)
                   END AS cache_hit_ratio
            FROM pg_stat_database
            WHERE datname IS NOT NULL
            ORDER BY datname
        """,
        "vacuum": """
            SELECT schemaname, relname, last_vacuum, last_autovacuum, last_analyze, last_autoanalyze,
                   n_dead_tup
            FROM pg_stat_user_tables
            ORDER BY n_dead_tup DESC
            LIMIT 20
        """,
        "index": """
            SELECT schemaname, relname, indexrelname, idx_scan, idx_tup_read, idx_tup_fetch
            FROM pg_stat_user_indexes
            ORDER BY idx_scan ASC, indexrelname
            LIMIT 20
        """,
        "sequence": """
            SELECT sequence_schema, sequence_name, data_type
            FROM information_schema.sequences
            ORDER BY sequence_schema, sequence_name
            LIMIT 100
        """,
        "replication": """
            SELECT application_name, state, sync_state, write_lag, flush_lag, replay_lag
            FROM pg_stat_replication
        """,
        "constraint": """
            SELECT connamespace::regnamespace::text AS schema, conrelid::regclass::text AS table_name,
                   conname, contype, convalidated
            FROM pg_constraint
            WHERE NOT convalidated
            ORDER BY conrelid::regclass::text, conname
        """,
    }

    def _run(self, health_type: str = "all") -> str:
        started = time.monotonic()
        checks = list(self._CHECKS) if health_type == "all" else [health_type]
        try:
            drv = _driver()
            reports: dict[str, Any] = {}
            total_duration = 0
            truncated = False
            masked: list[str] = []
            for check in checks:
                query = self._CHECKS.get(check)
                if not query:
                    continue
                result = drv.execute(query, readonly=True, max_rows=100)
                reports[check] = {"rows": result.rows, "row_count": result.row_count}
                total_duration += result.duration_ms
                truncated = truncated or result.truncated
                masked.extend(result.sensitive_fields_masked)
            payload, payload_truncated, payload_masked = limit_payload({"checks": reports})
            return dumps_result(
                make_result(
                    tool_name=self.name,
                    success=True,
                    result_type="health_report",
                    summary=f"Completed {len(reports)} PostgreSQL health check(s).",
                    payload=payload,
                    row_count=sum(report.get("row_count") or 0 for report in reports.values()),
                    duration_ms=total_duration or _timed_ms(started),
                    truncated=truncated or payload_truncated,
                    sensitive_fields_masked=[*masked, *payload_masked],
                )
            )
        except Exception as exc:
            return _error_result(self.name, "sql_error", exc, _timed_ms(started))


class LockInspectInput(BaseModel):
    limit: int = Field(default=50, ge=1, le=200, description="Maximum lock/activity rows to return.")


class PostgresLockInspectTool(AgentTool):
    name: str = "postgres_lock_inspect"
    description: str = "Inspect PostgreSQL lock waits, blocking sessions, long transactions, and idle-in-transaction sessions."
    args_schema: Type[BaseModel] = LockInspectInput
    tool_domain: str = "postgresql"
    operation_type: str = "diagnostic"
    risk_level: str = "low"
    read_only: bool = True
    destructive: bool = False
    allowed_phases: list[str] = ["observe", "diagnose", "verify", "report"]
    allowed_policies: list[str] = ["read_only_tools", "write_tools_after_approval"]
    output_type: str = "lock_report"
    result_sensitivity: str = "sensitive"
    search_hint: str | None = "inspect PostgreSQL locks blocking long transactions"

    def _run(self, limit: int = 50) -> str:
        sql = """
            SELECT a.pid,
                   a.state,
                   a.wait_event_type,
                   a.wait_event,
                   now() - a.xact_start AS xact_age,
                   pg_blocking_pids(a.pid) AS blocking_pids,
                   left(regexp_replace(a.query, '\\s+', ' ', 'g'), 300) AS query_preview
            FROM pg_stat_activity a
            WHERE a.wait_event IS NOT NULL
               OR cardinality(pg_blocking_pids(a.pid)) > 0
               OR a.state = 'idle in transaction'
            ORDER BY a.xact_start NULLS LAST
            LIMIT %s
        """
        started = time.monotonic()
        try:
            result = _driver().execute(sql, params=[limit], readonly=True, max_rows=limit)
            return dumps_result(
                make_result(
                    tool_name=self.name,
                    success=True,
                    result_type="lock_report",
                    summary=f"Collected {result.row_count} lock/activity row(s).",
                    payload={"activity": result.rows},
                    row_count=result.row_count,
                    duration_ms=result.duration_ms,
                    truncated=result.truncated,
                    sensitive_fields_masked=result.sensitive_fields_masked,
                )
            )
        except Exception as exc:
            return _error_result(self.name, "sql_error", exc, _timed_ms(started))


class IndexAdvisorInput(BaseModel):
    queries: list[str] = Field(description="One or more SELECT queries to analyze for index advice.", min_length=1, max_length=10)
    max_index_size_mb: int = Field(default=10_000, ge=1, le=1_000_000, description="Maximum acceptable total index size budget in MB.")


class PostgresIndexAdvisorTool(AgentTool):
    name: str = "postgres_index_advisor"
    description: str = "Generate lightweight PostgreSQL index advice from query predicates and explain evidence. Does not create indexes."
    args_schema: Type[BaseModel] = IndexAdvisorInput
    tool_domain: str = "postgresql"
    operation_type: str = "diagnostic"
    risk_level: str = "medium"
    read_only: bool = True
    destructive: bool = False
    allowed_phases: list[str] = ["observe", "diagnose", "propose", "verify"]
    allowed_policies: list[str] = ["read_only_tools", "write_tools_after_approval"]
    output_type: str = "index_advice"
    result_sensitivity: str = "internal"
    search_hint: str | None = "recommend PostgreSQL indexes from query workload"

    def _run(self, queries: list[str], max_index_size_mb: int = 10_000) -> str:
        started = time.monotonic()
        advice = []
        for query in queries:
            classification = classify_sql(query)
            if classification.primary_type != "read_only":
                advice.append({
                    "query_hash": classification.normalized_sql_hash,
                    "skipped": True,
                    "reason": f"Only read-only SELECT queries are supported, got {classification.primary_type}.",
                })
                continue
            candidates = _simple_index_candidates(query)
            advice.append({
                "query_hash": classification.normalized_sql_hash,
                "candidates": candidates,
                "warnings": classification.warnings,
                "max_index_size_mb": max_index_size_mb,
            })
        return dumps_result(
            make_result(
                tool_name=self.name,
                success=True,
                result_type="index_advice",
                summary=f"Generated lightweight index advice for {len(queries)} query/query(s).",
                payload={"advice": advice},
                row_count=len(advice),
                duration_ms=_timed_ms(started),
            )
        )


def _simple_index_candidates(query: str) -> list[dict[str, Any]]:
    import re

    table_match = re.search(r"\bfrom\s+([a-zA-Z_][\w\.]*)", query, re.IGNORECASE)
    table = table_match.group(1) if table_match else "unknown_table"
    columns = []
    for pattern in [
        r"\bwhere\s+([a-zA-Z_][\w]*)\s*(=|>|<|>=|<=|in\b|like\b)",
        r"\band\s+([a-zA-Z_][\w]*)\s*(=|>|<|>=|<=|in\b|like\b)",
        r"\border\s+by\s+([a-zA-Z_][\w]*)",
        r"\bjoin\s+[a-zA-Z_][\w\.]*\s+on\s+[a-zA-Z_][\w\.]*\.([a-zA-Z_][\w]*)",
    ]:
        for match in re.finditer(pattern, query, re.IGNORECASE):
            col = match.group(1)
            if col not in columns:
                columns.append(col)
    if not columns:
        return []
    return [
        {
            "table": table,
            "columns": [col],
            "index_method": "btree",
            "create_sql": f"CREATE INDEX CONCURRENTLY ON {table} ({col});",
            "requires_approval_to_apply": True,
            "evidence": ["Column appears in filter, join, or ordering predicate."],
            "warnings": ["Verify with postgres_hypothetical_index_test before applying."],
        }
        for col in columns[:5]
    ]


class HypotheticalIndexInput(BaseModel):
    sql: str = Field(description="SELECT query to explain with hypothetical indexes.", min_length=1, max_length=50_000)
    indexes: list[dict[str, Any]] = Field(default_factory=list, description="Index definitions with table, columns, and optional using.")


class PostgresHypotheticalIndexTestTool(AgentTool):
    name: str = "postgres_hypothetical_index_test"
    description: str = "Test hypothetical PostgreSQL indexes with HypoPG and compare plan cost without creating real indexes."
    args_schema: Type[BaseModel] = HypotheticalIndexInput
    tool_domain: str = "postgresql"
    operation_type: str = "diagnostic"
    risk_level: str = "medium"
    read_only: bool = True
    destructive: bool = False
    allowed_phases: list[str] = ["diagnose", "propose", "verify"]
    allowed_policies: list[str] = ["read_only_tools", "write_tools_after_approval"]
    output_type: str = "index_advice"
    result_sensitivity: str = "internal"
    search_hint: str | None = "simulate PostgreSQL hypothetical indexes with hypopg"

    def _run(self, sql: str, indexes: list[dict[str, Any]] | None = None) -> str:
        started = time.monotonic()
        classification = classify_sql(sql)
        if classification.primary_type != "read_only":
            return dumps_result(
                make_result(
                    tool_name=self.name,
                    success=False,
                    result_type="policy_denied",
                    summary=f"Hypothetical index test only supports read-only SELECT queries, got {classification.primary_type}.",
                    payload={"classification": classification.to_dict()},
                    duration_ms=_timed_ms(started),
                )
            )
        indexes = indexes or []
        try:
            drv = _driver()
            before = drv.execute(f"EXPLAIN (FORMAT JSON) {sql}", readonly=True, max_rows=1)
            if len(indexes) != 1:
                return dumps_result(
                    make_result(
                        tool_name=self.name,
                        success=False,
                        result_type="policy_denied",
                        summary="Hypothetical index test currently requires exactly one index definition.",
                        payload={"classification": classification.to_dict(), "index_count": len(indexes)},
                        duration_ms=_timed_ms(started),
                    )
                )
            definition = _index_definition(indexes[0])
            escaped_definition = definition.replace("'", "''")
            # HypoPG is session-local, so reset, create, explain, and reset again in one statement batch.
            multi_sql = (
                "SELECT hypopg_reset();\n"
                f"SELECT hypopg_create_index('{escaped_definition}');\n"
                f"EXPLAIN (FORMAT JSON) {sql};\n"
                "SELECT hypopg_reset();"
            )
            after = drv.execute(multi_sql, readonly=False, max_rows=5)
            before_plan = _summarize_plan(before.rows[0].get("QUERY PLAN") if before.rows else None)
            after_plan_raw = after.rows[-1].get("QUERY PLAN") if after.rows else None
            after_plan = _summarize_plan(after_plan_raw)
            before_cost = before_plan.get("total_cost")
            after_cost = after_plan.get("total_cost")
            improvement = None
            if isinstance(before_cost, (int, float)) and isinstance(after_cost, (int, float)) and after_cost:
                improvement = before_cost / after_cost
            return dumps_result(
                make_result(
                    tool_name=self.name,
                    success=True,
                    result_type="index_advice",
                    summary=f"Hypothetical index plan comparison completed. improvement={improvement}.",
                    payload={
                        "classification": classification.to_dict(),
                        "before_plan": before_plan,
                        "after_plan": after_plan,
                        "improvement_ratio": improvement,
                    },
                    row_count=after.row_count,
                    duration_ms=before.duration_ms + after.duration_ms,
                    truncated=before.truncated or after.truncated,
                    sensitive_fields_masked=[*before.sensitive_fields_masked, *after.sensitive_fields_masked],
                )
            )
        except Exception as exc:
            return _error_result(self.name, "sql_error", exc, _timed_ms(started))


def _index_definition(index: dict[str, Any]) -> str:
    table = str(index.get("table", "")).strip()
    columns = index.get("columns", [])
    using = str(index.get("using", "btree")).strip() or "btree"
    if not table or not isinstance(columns, list) or not columns:
        raise ValueError("Hypothetical index requires table and non-empty columns.")
    if using.lower() not in {"btree", "hash", "gist", "gin", "brin", "spgist"}:
        raise ValueError(f"Unsupported index method: {using}")
    safe_columns = ", ".join(_quote_ident(str(col).strip()) for col in columns)
    return f"CREATE INDEX ON {_quote_qualified_name(table)} USING {using.lower()} ({safe_columns})"


class DryRunInput(BaseModel):
    sql: str = Field(description="Write SQL to validate in a rollback-only dry run.", min_length=1, max_length=50_000)


class PostgresDryRunTool(AgentTool):
    name: str = "postgres_dry_run"
    description: str = "Validate write SQL risk and estimate impact before approval. Does not commit changes."
    args_schema: Type[BaseModel] = DryRunInput
    tool_domain: str = "postgresql"
    operation_type: str = "diagnostic"
    risk_level: str = "high"
    read_only: bool = False
    destructive: bool = False
    requires_approval: bool = False
    requires_transaction: bool = True
    allowed_phases: list[str] = ["approve", "execute"]
    allowed_policies: list[str] = ["write_tools_after_approval"]
    output_type: str = "dry_run_report"
    result_sensitivity: str = "sensitive"
    supports_parallel: bool = False
    search_hint: str | None = "dry run PostgreSQL write SQL with rollback"

    def _run(self, sql: str) -> str:
        started = time.monotonic()
        classification = classify_sql(sql, allow_explain_analyze=True)
        if not classification.destructive:
            return dumps_result(
                make_result(
                    tool_name=self.name,
                    success=False,
                    result_type="policy_denied",
                    summary="Dry run is only for write, schema, permission, or maintenance SQL.",
                    payload={"classification": classification.to_dict()},
                    duration_ms=_timed_ms(started),
                )
            )
        # First-stage dry-run is deliberately non-executing. It produces the approval evidence contract.
        return dumps_result(
            make_result(
                tool_name=self.name,
                success=True,
                result_type="dry_run_report",
                summary=f"Dry-run report prepared for {classification.primary_type}; execution is not committed.",
                payload={
                    "classification": classification.to_dict(),
                    "sql_hash": classification.normalized_sql_hash,
                    "impact_summary": "Impact must be reviewed by the user before execution.",
                    "rollback_summary": "Use an explicit transaction, backup, or inverse migration where applicable.",
                    "expected_affected_rows": None,
                    "execution_performed": False,
                },
                duration_ms=_timed_ms(started),
            )
        )


class ExecuteWriteInput(BaseModel):
    sql: str = Field(description="Approved write SQL to execute.", min_length=1, max_length=50_000)
    approval_id: str = Field(description="Approval id bound to this SQL and step.", min_length=1)
    approved_sql_hash: str = Field(description="SQL hash that was approved.", min_length=8)
    target_environment: str = Field(default="unknown", description="Target environment for audit.")
    impact_summary: str = Field(description="User-reviewed impact summary.", min_length=1)
    rollback_summary: str = Field(description="User-reviewed rollback summary.", min_length=1)


class PostgresExecuteWriteTool(AgentTool):
    name: str = "postgres_execute_write"
    description: str = "Execute approved PostgreSQL write SQL after verifying approval-bound SQL hash and required impact metadata."
    args_schema: Type[BaseModel] = ExecuteWriteInput
    tool_domain: str = "postgresql"
    operation_type: str = "data_change"
    risk_level: str = "high"
    read_only: bool = False
    destructive: bool = True
    requires_approval: bool = True
    requires_transaction: bool = True
    allowed_phases: list[str] = ["execute"]
    allowed_policies: list[str] = ["write_tools_after_approval"]
    output_type: str = "write_result"
    result_sensitivity: str = "sensitive"
    supports_parallel: bool = False
    search_hint: str | None = "execute approved PostgreSQL write SQL"

    def _run(
        self,
        sql: str,
        approval_id: str,
        approved_sql_hash: str,
        target_environment: str = "unknown",
        impact_summary: str = "",
        rollback_summary: str = "",
    ) -> str:
        started = time.monotonic()
        blocked = _write_blocked_by_environment()
        if blocked:
            return dumps_result(
                make_result(
                    tool_name=self.name,
                    success=False,
                    result_type="policy_denied",
                    summary=blocked,
                    payload={"target_environment": target_environment},
                    duration_ms=_timed_ms(started),
                )
            )
        classification = classify_sql(sql, allow_explain_analyze=True)
        if classification.normalized_sql_hash != approved_sql_hash:
            return dumps_result(
                make_result(
                    tool_name=self.name,
                    success=False,
                    result_type="policy_denied",
                    summary="Approved SQL hash does not match current SQL.",
                    payload={"classification": classification.to_dict(), "approved_sql_hash": approved_sql_hash},
                    duration_ms=_timed_ms(started),
                )
            )
        if not classification.destructive:
            return dumps_result(
                make_result(
                    tool_name=self.name,
                    success=False,
                    result_type="policy_denied",
                    summary="Write execution tool only accepts destructive or maintenance SQL.",
                    payload={"classification": classification.to_dict()},
                    duration_ms=_timed_ms(started),
                )
            )
        try:
            requires_autocommit = bool(
                re.search(r"\bcreate\s+index\s+concurrently\b", sql, re.IGNORECASE)
            )
            result = _driver().execute(
                sql,
                readonly=False,
                max_rows=100,
                autocommit=requires_autocommit,
                statement_timeout_ms=120_000 if requires_autocommit else None,
            )
            return dumps_result(
                make_result(
                    tool_name=self.name,
                    success=True,
                    result_type="write_result",
                    summary=f"Approved SQL executed. affected_rows={result.affected_rows}.",
                    payload={
                        "classification": classification.to_dict(),
                        "approval_id": approval_id,
                        "target_environment": target_environment,
                        "impact_summary": impact_summary,
                        "rollback_summary": rollback_summary,
                        "rows": result.rows,
                    },
                    row_count=result.row_count,
                    affected_rows=result.affected_rows,
                    duration_ms=result.duration_ms,
                    truncated=result.truncated,
                    sensitive_fields_masked=result.sensitive_fields_masked,
                )
            )
        except Exception as exc:
            return _error_result(self.name, "sql_error", exc, _timed_ms(started))


class MaintenanceInput(BaseModel):
    schema_name: str = Field(description="Schema name.", min_length=1, max_length=256)
    table_name: str = Field(description="Table name.", min_length=1, max_length=256)


class PostgresAnalyzeTableTool(AgentTool):
    name: str = "postgres_analyze_table"
    description: str = "Run ANALYZE on a PostgreSQL table after approval."
    args_schema: Type[BaseModel] = MaintenanceInput
    tool_domain: str = "postgresql"
    operation_type: str = "maintenance"
    risk_level: str = "high"
    read_only: bool = False
    destructive: bool = False
    requires_approval: bool = True
    allowed_phases: list[str] = ["execute", "verify"]
    allowed_policies: list[str] = ["write_tools_after_approval"]
    output_type: str = "maintenance_result"
    result_sensitivity: str = "internal"
    supports_parallel: bool = False

    def _run(self, schema_name: str, table_name: str) -> str:
        return _maintenance_result(self.name, f"ANALYZE {_quote_ident(schema_name)}.{_quote_ident(table_name)}")


class PostgresVacuumTableTool(AgentTool):
    name: str = "postgres_vacuum_table"
    description: str = "Run VACUUM on a PostgreSQL table after approval."
    args_schema: Type[BaseModel] = MaintenanceInput
    tool_domain: str = "postgresql"
    operation_type: str = "maintenance"
    risk_level: str = "high"
    read_only: bool = False
    destructive: bool = False
    requires_approval: bool = True
    allowed_phases: list[str] = ["execute", "verify"]
    allowed_policies: list[str] = ["write_tools_after_approval"]
    output_type: str = "maintenance_result"
    result_sensitivity: str = "internal"
    supports_parallel: bool = False

    def _run(self, schema_name: str, table_name: str) -> str:
        return _maintenance_result(self.name, f"VACUUM {_quote_ident(schema_name)}.{_quote_ident(table_name)}")


class CreateIndexInput(BaseModel):
    table_name: str = Field(description="Qualified or unqualified table name.", min_length=1)
    columns: list[str] = Field(description="Columns to index.", min_length=1, max_length=5)
    index_name: Optional[str] = Field(default=None, description="Optional index name.")
    using: str = Field(default="btree", description="Index method.")


class PostgresCreateIndexConcurrentlyTool(AgentTool):
    name: str = "postgres_create_index_concurrently"
    description: str = "Create a PostgreSQL index concurrently after approval."
    args_schema: Type[BaseModel] = CreateIndexInput
    tool_domain: str = "postgresql"
    operation_type: str = "schema_change"
    risk_level: str = "high"
    read_only: bool = False
    destructive: bool = True
    requires_approval: bool = True
    allowed_phases: list[str] = ["execute", "verify"]
    allowed_policies: list[str] = ["write_tools_after_approval"]
    output_type: str = "maintenance_result"
    result_sensitivity: str = "internal"
    supports_parallel: bool = False

    def _run(self, table_name: str, columns: list[str], index_name: Optional[str] = None, using: str = "btree") -> str:
        if using.lower() not in {"btree", "hash", "gist", "gin", "brin", "spgist"}:
            return _error_result(self.name, "tool_error", ValueError(f"Unsupported index method: {using}"))
        idx = f"{_quote_ident(index_name)} " if index_name else ""
        cols = ", ".join(_quote_ident(col) for col in columns)
        sql = f"CREATE INDEX CONCURRENTLY {idx}ON {_quote_qualified_name(table_name)} USING {using.lower()} ({cols})"
        return _maintenance_result(self.name, sql)


def _maintenance_result(tool_name: str, sql: str) -> str:
    started = time.monotonic()
    blocked = _write_blocked_by_environment()
    if blocked:
        return dumps_result(
            make_result(
                tool_name=tool_name,
                success=False,
                result_type="policy_denied",
                summary=blocked,
                payload={},
                duration_ms=_timed_ms(started),
            )
        )
    classification = classify_sql(sql, allow_explain_analyze=True)
    try:
        result = _driver().execute(sql, readonly=False, max_rows=20, statement_timeout_ms=120_000, autocommit=True)
        return dumps_result(
            make_result(
                tool_name=tool_name,
                success=True,
                result_type="maintenance_result",
                summary=f"Maintenance SQL executed: {classification.primary_type}.",
                payload={"classification": classification.to_dict(), "sql_hash": classification.normalized_sql_hash},
                row_count=result.row_count,
                affected_rows=result.affected_rows,
                duration_ms=result.duration_ms,
                truncated=result.truncated,
                sensitive_fields_masked=result.sensitive_fields_masked,
            )
        )
    except Exception as exc:
        return _error_result(tool_name, "sql_error", exc, _timed_ms(started))
