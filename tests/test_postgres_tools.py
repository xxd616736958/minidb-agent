"""Tests for PostgreSQL-specific tool implementation."""

from __future__ import annotations

from datetime import datetime, timezone

from langchain_core.messages import ToolMessage

from agent.nodes.agent_loop import normalize_observation
from agent.nodes.tool_executor import _tool_execution_result
from tools.builtin.postgres import (
    PostgresDryRunTool,
    PostgresExplainTool,
    PostgresIndexAdvisorTool,
    PostgresQueryReadonlyTool,
    PostgresSQLClassifyTool,
)
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


def test_expected_postgres_read_alias_exposes_new_read_tools():
    registry = SkillRegistry()
    registry.register(PostgresQueryReadonlyTool())
    registry.register(PostgresExplainTool())
    registry.register(PostgresDryRunTool())

    tools, specs = registry.get_for_state(_state())
    names = {tool.name for tool in tools}

    assert "postgres_query_readonly" in names
    assert "postgres_explain" in names
    assert "postgres_dry_run" not in names
    assert all(spec["capability"]["read_only"] for spec in specs)


def test_dry_run_is_available_before_write_approval_but_execute_write_is_not():
    registry = SkillRegistry()
    registry.register(PostgresDryRunTool())
    from tools.builtin.postgres import PostgresExecuteWriteTool

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
    assert "postgres_execute_write" not in names


def test_readonly_tool_rejects_write_sql_without_database_connection():
    output = PostgresQueryReadonlyTool()._run("UPDATE orders SET status = 'done'")
    result = loads_result(output)

    assert result is not None
    assert result["success"] is False
    assert result["result_type"] == "policy_denied"


def test_sql_classify_tool_returns_structured_result():
    output = PostgresSQLClassifyTool()._run("SELECT 1")
    result = loads_result(output)

    assert result is not None
    assert result["result_type"] == "sql_classification"
    assert result["payload"]["primary_type"] == "read_only"


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
