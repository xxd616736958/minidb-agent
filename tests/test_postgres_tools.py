"""Tests for PostgreSQL-specific tool implementation."""

from __future__ import annotations

import sys
import types
from datetime import datetime, timezone

from langchain_core.messages import ToolMessage

from agent.nodes.agent_loop import normalize_observation
from agent.nodes.tool_executor import _tool_execution_result
from tools.builtin.postgres import (
    PostgresDryRunTool,
    PostgresConnectionCheckTool,
    PostgresCreateIndexConcurrentlyTool,
    PostgresExecuteWriteTool,
    PostgresExplainTool,
    PostgresIndexAdvisorTool,
    PostgresListDatabasesTool,
    PostgresListObjectsTool,
    PostgresListSchemasTool,
    PostgresObjectDetailTool,
    PostgresQueryReadonlyTool,
    PostgresSchemaOverviewTool,
    PostgresSQLClassifyTool,
    PostgresTopQueriesTool,
)
from tools.builtin.code_search import CodeSearchTool
from tools.builtin.file_read import FileReadTool
from tools.postgres.driver import PostgresConnectionManager, PostgresDriver, QueryResult
from tools.postgres.results import dumps_result, loads_result, make_result
from tools.postgres.sanitizer import limit_rows, obfuscate_password
from tools.postgres.sql_safety import classify_sql
from tools.policy import evaluate_tool_call
from tools.registry import SkillRegistry


def _step(**extra):
    step = {
        "id": "observe",
        "description": "Observe database",
        "status": "running",
        "dependencies": [],
        "result": None,
        "error": None,
        "phase": "observe",
        "operation_type": "diagnostic",
        "risk_level": "low",
        "requires_approval": False,
        "requires_rollback_plan": False,
        "evidence_required": [],
        "success_criteria": ["done"],
        "expected_tools": ["postgres_read"],
        "tool_policy": "read_only_tools",
    }
    step.update(extra)
    return step


def _state(**extra):
    state = {
        "messages": [],
        "task_stack": [_step()],
        "current_task_index": 0,
        "current_step_id": "observe",
        "approval_decisions": [],
        "db_observations": [],
        "tool_execution_results": [],
        "database_environment": {
            "environment_name": "dev",
            "target_database": "db_agent",
            "safe_host_label": "127.0.0.1",
            "safe_user_label": "db_agent",
            "access_mode": "write_after_approval",
            "is_production": False,
            "allow_write_tools": True,
        },
        "runtime_policy": {
            "allow_database_writes": True,
            "require_approval_for_database_write": True,
        },
    }
    state.update(extra)
    return state


def test_sql_classifier_marks_read_and_write_risk():
    read = classify_sql("SELECT * FROM orders WHERE id = 1")
    write = classify_sql("DELETE FROM orders")

    assert read.primary_type == "read_only"
    assert read.read_only is True
    assert write.primary_type == "data_change"
    assert write.requires_approval is True
    assert write.risk_level == "critical"


def test_sql_classifier_blocks_explain_analyze_by_default():
    classification = classify_sql("EXPLAIN ANALYZE SELECT * FROM orders")

    assert classification.requires_approval is True
    assert any("EXPLAIN ANALYZE" in reason for reason in classification.blocked_reasons)


def test_sanitizer_masks_passwords_and_sensitive_columns():
    assert obfuscate_password("postgresql://user:secret@localhost/db") == "postgresql://user:****@localhost/db"

    rows, truncated, masked = limit_rows(
        [{"email": "a@example.com", "name": "Ann", "token": "abc", "note": "x" * 600}],
        max_rows=1,
        max_cell_chars=10,
    )

    assert rows[0]["email"] == "***MASKED***"
    assert rows[0]["token"] == "***MASKED***"
    assert rows[0]["note"].endswith("[truncated]")
    assert truncated is True
    assert any(field.endswith("email") for field in masked)


def test_postgres_tools_are_discovered_with_metadata():
    registry = SkillRegistry()
    registry.discover("tools.builtin")

    spec = registry.get_spec("postgres_query_readonly")
    assert spec is not None
    assert spec["capability"]["domain"] == "postgresql"
    assert spec["capability"]["read_only"] is True

    write_spec = registry.get_spec("postgres_execute_write")
    assert write_spec is not None
    assert write_spec["capability"]["requires_approval"] is True

    db_spec = registry.get_spec("postgres_list_databases")
    assert db_spec is not None
    assert db_spec["capability"]["read_only"] is True

    overview_spec = registry.get_spec("postgres_schema_overview")
    assert overview_spec is not None
    assert overview_spec["capability"]["read_only"] is True


def test_expected_postgres_read_alias_exposes_new_read_tools():
    registry = SkillRegistry()
    registry.register(PostgresQueryReadonlyTool())
    registry.register(PostgresListDatabasesTool())
    registry.register(PostgresExplainTool())
    registry.register(PostgresSchemaOverviewTool())
    registry.register(PostgresDryRunTool())

    tools, specs = registry.get_for_state(_state())
    names = {tool.name for tool in tools}

    assert "postgres_query_readonly" in names
    assert "postgres_list_databases" in names
    assert "postgres_explain" in names
    assert "postgres_schema_overview" in names
    assert "postgres_dry_run" not in names
    assert all(spec["capability"]["read_only"] for spec in specs)


def test_schema_overview_returns_connection_schemas_and_tables_without_percent_placeholder(monkeypatch):
    captured_sql: list[str] = []

    class FakeDriver:
        def execute(self, sql, *, params=None, readonly=False, max_rows=100, **kwargs):
            captured_sql.append(sql)
            assert readonly is True
            if "current_database()" in sql:
                return QueryResult(
                    rows=[{"database": "db_agent", "user": "db_agent", "host": "127.0.0.1", "port": 5432}],
                    row_count=1,
                    affected_rows=None,
                    sqlstate=None,
                    duration_ms=1,
                    truncated=False,
                    sensitive_fields_masked=[],
                )
            if "information_schema.schemata" in sql:
                return QueryResult(
                    rows=[{"schema_name": "public", "schema_owner": "db_agent", "schema_type": "user"}],
                    row_count=1,
                    affected_rows=None,
                    sqlstate=None,
                    duration_ms=1,
                    truncated=False,
                    sensitive_fields_masked=[],
                )
            assert "information_schema.tables" in sql
            return QueryResult(
                rows=[{"schema": "public", "name": "big_orders_demo", "type": "BASE TABLE"}],
                row_count=1,
                affected_rows=None,
                sqlstate=None,
                duration_ms=1,
                truncated=False,
                sensitive_fields_masked=[],
            )

    monkeypatch.setattr("tools.builtin.postgres._driver", lambda: FakeDriver())

    output = PostgresSchemaOverviewTool()._run(include_system=False, table_limit=10)
    result = loads_result(output)

    assert result is not None
    assert result["success"] is True
    assert result["payload"]["connection"]["database"] == "db_agent"
    assert result["payload"]["schemas"][0]["schema_name"] == "public"
    assert result["payload"]["tables"][0]["name"] == "big_orders_demo"
    assert not any("LIKE 'pg_%" in sql for sql in captured_sql)


def test_write_tools_are_visible_before_approval_so_policy_gate_can_prompt():
    registry = SkillRegistry()
    registry.register(PostgresDryRunTool())
    registry.register(PostgresExecuteWriteTool())
    state = _state(
        task_stack=[
            _step(
                id="execute",
                phase="execute",
                expected_tools=["postgres_write"],
                tool_policy="write_tools_after_approval",
            )
        ],
        current_step_id="execute",
    )
    tools, _ = registry.get_for_state(state)
    names = {tool.name for tool in tools}

    assert "postgres_dry_run" in names
    assert "postgres_execute_write" in names


def test_postgres_step_hides_generic_code_and_file_tools():
    registry = SkillRegistry()
    registry.register(PostgresConnectionCheckTool())
    registry.register(PostgresTopQueriesTool())
    registry.register(CodeSearchTool())
    registry.register(FileReadTool())
    state = _state(
        current_intent={
            "domain": "postgresql",
            "primary_intent": "performance_diagnosis",
            "operation_nature": "diagnostic",
        },
        task_stack=[
            _step(
                id="collect-top-queries",
                phase="observe",
                expected_tools=["postgres_connection_check", "postgres_top_queries"],
                tool_policy="read_only_tools",
            )
        ],
        current_step_id="collect-top-queries",
    )

    tools, _ = registry.get_for_state(state)
    names = {tool.name for tool in tools}

    assert "postgres_connection_check" in names
    assert "postgres_top_queries" in names
    assert "code_search" not in names
    assert "file_read" not in names


def test_readonly_tool_rejects_write_sql_without_database_connection():
    output = PostgresQueryReadonlyTool()._run("UPDATE orders SET status = 'done'")
    result = loads_result(output)

    assert result is not None
    assert result["success"] is False
    assert result["result_type"] == "policy_denied"


def test_postgres_explain_downgrades_analyze_to_safe_explain(monkeypatch):
    captured = {}

    class FakeDriver:
        def execute(self, sql, *, readonly=False, max_rows=100, **kwargs):
            captured["sql"] = sql
            captured["readonly"] = readonly
            return QueryResult(
                rows=[{"QUERY PLAN": [{"Plan": {"Node Type": "Seq Scan", "Total Cost": 10.0, "Plan Rows": 100}}]}],
                row_count=1,
                affected_rows=None,
                sqlstate=None,
                duration_ms=1,
                truncated=False,
                sensitive_fields_masked=[],
            )

    monkeypatch.setattr("tools.builtin.postgres._driver", lambda: FakeDriver())

    output = PostgresExplainTool()._run("SELECT * FROM orders", analyze=True)
    result = loads_result(output)

    assert result is not None
    assert result["success"] is True
    assert result["result_type"] == "explain_plan"
    assert result["payload"]["analyze_requested"] is True
    assert result["payload"]["analyze_executed"] is False
    assert "EXPLAIN (FORMAT JSON)" in captured["sql"]
    assert "ANALYZE" not in captured["sql"]


def test_sql_classify_tool_returns_structured_result():
    output = PostgresSQLClassifyTool()._run("SELECT 1")
    result = loads_result(output)

    assert result is not None
    assert result["result_type"] == "sql_classification"
    assert result["payload"]["primary_type"] == "read_only"


def test_postgres_driver_sets_timeouts_with_set_config(monkeypatch):
    calls = []
    autocommit_values = []

    class FakeCursor:
        description = (("one",),)
        rowcount = 1

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, sql, params=None):
            calls.append((sql, params))

        def fetchall(self):
            return [{"one": 1}]

    class FakeConnection:
        def __setattr__(self, name, value):
            if name == "autocommit":
                autocommit_values.append(value)
            super().__setattr__(name, value)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def cursor(self):
            return FakeCursor()

        def rollback(self):
            pass

        def commit(self):
            pass

    fake_psycopg = types.ModuleType("psycopg")
    fake_psycopg.connect = lambda *args, **kwargs: FakeConnection()
    fake_rows = types.ModuleType("psycopg.rows")
    fake_rows.dict_row = object()
    monkeypatch.setitem(sys.modules, "psycopg", fake_psycopg)
    monkeypatch.setitem(sys.modules, "psycopg.rows", fake_rows)

    manager = PostgresConnectionManager("postgresql://u:p@localhost:5432/app")
    driver = PostgresDriver(manager)
    result = driver.execute("SELECT 1 AS one", readonly=True, statement_timeout_ms=1234, lock_timeout_ms=234)

    assert result.rows == [{"one": 1}]
    assert calls[0] == ("SELECT set_config('statement_timeout', %s, %s)", ("1234", True))
    assert calls[1] == ("SELECT set_config('lock_timeout', %s, %s)", ("234", True))
    assert not any(sql.startswith("SET statement_timeout") for sql, _ in calls)
    assert autocommit_values == []


def test_postgres_driver_can_use_autocommit_for_non_transactional_maintenance(monkeypatch):
    calls = []
    autocommit_values = []
    commits = []

    class FakeCursor:
        description = None
        rowcount = -1

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, sql, params=None):
            calls.append((sql, params))

    class FakeConnection:
        def __setattr__(self, name, value):
            if name == "autocommit":
                autocommit_values.append(value)
            super().__setattr__(name, value)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def cursor(self):
            return FakeCursor()

        def rollback(self):
            commits.append("rollback")

        def commit(self):
            commits.append("commit")

    fake_psycopg = types.ModuleType("psycopg")
    fake_psycopg.connect = lambda *args, **kwargs: FakeConnection()
    fake_rows = types.ModuleType("psycopg.rows")
    fake_rows.dict_row = object()
    monkeypatch.setitem(sys.modules, "psycopg", fake_psycopg)
    monkeypatch.setitem(sys.modules, "psycopg.rows", fake_rows)

    manager = PostgresConnectionManager("postgresql://u:p@localhost:5432/app")
    driver = PostgresDriver(manager)
    driver.execute("CREATE INDEX CONCURRENTLY idx ON orders (id)", autocommit=True)

    assert autocommit_values == [True]
    assert calls[0] == ("SELECT set_config('statement_timeout', %s, %s)", ("30000", False))
    assert calls[1] == ("SELECT set_config('lock_timeout', %s, %s)", ("5000", False))
    assert calls[2][0].startswith("CREATE INDEX CONCURRENTLY")
    assert commits == []


def test_list_schemas_sql_avoids_percent_literals(monkeypatch):
    captured = {}

    class FakeDriver:
        def execute(self, sql, *, params=None, readonly=False, max_rows=100, **kwargs):
            captured["sql"] = sql
            captured["params"] = params
            return QueryResult(
                rows=[{"schema_name": "public", "schema_owner": "db_agent", "schema_type": "user"}],
                row_count=1,
                affected_rows=None,
                sqlstate=None,
                duration_ms=1,
                truncated=False,
                sensitive_fields_masked=[],
            )

    monkeypatch.setattr("tools.builtin.postgres._driver", lambda: FakeDriver())

    output = PostgresListSchemasTool()._run(include_system=False)
    result = loads_result(output)

    assert result is not None
    assert result["success"] is True
    assert "LIKE 'pg_%" not in captured["sql"]
    assert "starts_with(schema_name, 'pg_')" in captured["sql"]
    assert captured["params"] == [False]


def test_postgres_object_tools_accept_model_friendly_schema_aliases():
    list_args = PostgresListObjectsTool().args_schema.model_validate({"schema": "public", "object_type": "table"})
    detail_args = PostgresObjectDetailTool().args_schema.model_validate({"schema": "public", "table": "users"})

    assert list_args.schema_name == "public"
    assert detail_args.schema_name == "public"
    assert detail_args.object_name == "users"


def test_policy_allows_dry_run_without_approval_when_registered_globally():
    from tools.registry import registry

    registry.clear()
    registry.register(PostgresDryRunTool())
    state = _state(
        task_stack=[
            _step(
                id="execute",
                phase="execute",
                expected_tools=["postgres_write"],
                tool_policy="write_tools_after_approval",
            )
        ],
        current_step_id="execute",
    )

    decision = evaluate_tool_call(
        state,
        {"name": "postgres_dry_run", "args": {"sql": "UPDATE orders SET status='x' WHERE id = 1"}, "id": "call-dry"},
    )

    assert decision["decision"] == "allow"


def test_index_advisor_returns_candidates_without_database():
    output = PostgresIndexAdvisorTool()._run(["SELECT * FROM orders WHERE user_id = 1 ORDER BY created_at"])
    result = loads_result(output)

    assert result is not None
    candidates = result["payload"]["advice"][0]["candidates"]
    assert any(candidate["columns"] == ["user_id"] for candidate in candidates)


def test_create_index_concurrently_tool_uses_autocommit(monkeypatch):
    captured = {}

    class FakeDriver:
        def execute(self, sql, *, autocommit=False, readonly=False, max_rows=100, statement_timeout_ms=None, **kwargs):
            captured.update(
                {
                    "sql": sql,
                    "autocommit": autocommit,
                    "readonly": readonly,
                    "statement_timeout_ms": statement_timeout_ms,
                }
            )
            return QueryResult(
                rows=[],
                row_count=0,
                affected_rows=None,
                sqlstate=None,
                duration_ms=1,
                truncated=False,
                sensitive_fields_masked=[],
            )

    monkeypatch.setattr("tools.builtin.postgres._driver", lambda: FakeDriver())
    monkeypatch.setenv("POSTGRES_TARGET_ENV", "dev")

    output = PostgresCreateIndexConcurrentlyTool()._run(
        table_name="public.orders",
        columns=["created_at"],
        index_name="idx_orders_created_at",
    )
    result = loads_result(output)

    assert result is not None
    assert result["success"] is True
    assert "CREATE INDEX CONCURRENTLY" in captured["sql"]
    assert captured["autocommit"] is True
    assert captured["readonly"] is False
    assert captured["statement_timeout_ms"] == 120_000


def test_execute_write_tool_uses_autocommit_for_create_index_concurrently(monkeypatch):
    captured = {}
    sql = "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_orders_status ON public.orders (status)"

    class FakeDriver:
        def execute(self, sql_text, *, autocommit=False, readonly=False, max_rows=100, statement_timeout_ms=None, **kwargs):
            captured.update(
                {
                    "sql": sql_text,
                    "autocommit": autocommit,
                    "readonly": readonly,
                    "statement_timeout_ms": statement_timeout_ms,
                }
            )
            return QueryResult(
                rows=[],
                row_count=0,
                affected_rows=None,
                sqlstate=None,
                duration_ms=1,
                truncated=False,
                sensitive_fields_masked=[],
            )

    monkeypatch.setattr("tools.builtin.postgres._driver", lambda: FakeDriver())
    monkeypatch.setenv("POSTGRES_TARGET_ENV", "dev")

    output = PostgresExecuteWriteTool()._run(
        sql,
        approval_id="approval-1",
        approved_sql_hash=classify_sql(sql, allow_explain_analyze=True).normalized_sql_hash,
        target_environment="dev",
        impact_summary="Create index.",
        rollback_summary="Drop index.",
    )
    result = loads_result(output)

    assert result is not None
    assert result["success"] is True
    assert captured["autocommit"] is True
    assert captured["readonly"] is False
    assert captured["statement_timeout_ms"] == 120_000


def test_top_queries_degrades_when_pg_stat_statements_is_missing(monkeypatch):
    class FakeDriver:
        def execute(self, sql, *, params=None, readonly=False, max_rows=100, **kwargs):
            if "pg_extension" in sql:
                return QueryResult(
                    rows=[{"installed": False}],
                    row_count=1,
                    affected_rows=None,
                    sqlstate=None,
                    duration_ms=1,
                    truncated=False,
                    sensitive_fields_masked=[],
                )
            assert "pg_stat_activity" in sql
            return QueryResult(
                rows=[{"pid": 1, "state": "active", "query_preview": "select 1"}],
                row_count=1,
                affected_rows=None,
                sqlstate=None,
                duration_ms=2,
                truncated=False,
                sensitive_fields_masked=[],
            )

    monkeypatch.setattr("tools.builtin.postgres._driver", lambda: FakeDriver())

    output = PostgresTopQueriesTool()._run(limit=5)
    result = loads_result(output)

    assert result is not None
    assert result["success"] is True
    assert result["result_type"] == "top_queries"
    assert result["payload"]["history_available"] is False
    assert result["payload"]["active_queries"][0]["query_preview"] == "select 1"


def test_structured_postgres_result_feeds_tool_execution_result_and_observation():
    content = dumps_result(
        make_result(
            tool_name="postgres_health_check",
            success=True,
            result_type="health_report",
            summary="Health check completed.",
            payload={"checks": {"connection": {"row_count": 1}}},
            row_count=1,
            duration_ms=12,
        )
    )
    msg = ToolMessage(content=content, name="postgres_health_check", tool_call_id="call-pg-1")
    result = _tool_execution_result(msg, datetime.now(timezone.utc))

    assert result["result_type"] == "health_report"
    assert result["payload"]["checks"]["connection"]["row_count"] == 1

    update = normalize_observation(_state(tool_execution_results=[result]))
    assert update["db_observations"][0]["type"] == "health_report"
    assert update["db_observations"][0]["payload"]["checks"]["connection"]["row_count"] == 1
