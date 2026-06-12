"""Tests for execution environment and workspace management."""

from __future__ import annotations

import os
from datetime import datetime, timezone

from langchain_core.messages import ToolMessage

from agent.context import build_prompt_context
from agent.nodes.tool_executor import (
    _artifact_kind_for_result,
    _tool_execution_result,
    _update_invocation_records,
)
from execution.environment import (
    DatabaseEnvironmentManager,
    ExecutionEnvironmentManager,
    WorkspaceManager,
    build_database_environment_profile,
    build_runtime_policy,
    build_workspace_profile,
)
from tools.builtin.postgres import PostgresDryRunTool, PostgresExecuteWriteTool
from tools.builtin.shell import ShellTool
from tools.postgres.results import dumps_result, loads_result, make_result
from tools.postgres.sql_safety import classify_sql
from tools.registry import SkillRegistry


def _step(**extra):
    step = {
        "id": "execute",
        "description": "Execute",
        "status": "running",
        "dependencies": [],
        "result": None,
        "error": None,
        "phase": "execute",
        "operation_type": "data_change",
        "risk_level": "high",
        "requires_approval": True,
        "requires_rollback_plan": True,
        "evidence_required": [],
        "success_criteria": ["done"],
        "expected_tools": ["postgres_write"],
        "tool_policy": "write_tools_after_approval",
    }
    step.update(extra)
    return step


def _state(**extra):
    state = {
        "messages": [],
        "task_stack": [_step()],
        "current_task_index": 0,
        "current_step_id": "execute",
        "approval_decisions": [],
    }
    state.update(extra)
    return state


def test_workspace_manager_blocks_write_outside_root(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    profile = build_workspace_profile(str(root))
    manager = WorkspaceManager(profile)

    inside = manager.resolve_for_write("reports/out.md")
    assert str(inside).startswith(str(root))

    outside = tmp_path / "outside.md"
    try:
        manager.resolve_for_write(str(outside))
    except PermissionError as exc:
        assert "outside workspace" in str(exc)
    else:
        raise AssertionError("Expected outside write to be blocked")


def test_execution_environment_bootstrap_creates_task_workspace(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    manager = ExecutionEnvironmentManager(_state())
    update = manager.bootstrap_state()

    assert update["workspace_profile"]["root_path"] == str(tmp_path)
    assert update["task_workspace"]["root_path"].startswith(str(tmp_path / ".mini_agent" / "tasks"))
    assert os.path.isdir(update["task_workspace"]["root_path"])


def test_database_environment_profile_is_safe_and_production_aware(monkeypatch):
    monkeypatch.setenv("POSTGRES_TARGET_ENV", "production")
    profile = build_database_environment_profile("postgresql://user:secret@prod.example.com:5432/app")

    assert profile["environment_name"] == "production"
    assert profile["is_production"] is True
    assert profile["safe_host_label"] == "prod.example.com"
    assert profile["safe_user_label"] == "user"
    assert "secret" not in str(profile)


def test_database_environment_manager_exposes_session_policies(monkeypatch):
    monkeypatch.setenv("POSTGRES_TARGET_ENV", "dev")
    profile = build_database_environment_profile("postgresql://user:secret@dev.example.com:5432/app")
    manager = DatabaseEnvironmentManager(profile)

    readonly = manager.readonly_session()
    diagnostic = manager.diagnostic_session()
    write = manager.write_session(
        approval_id="approval-1",
        sql_hash="abc12345",
        rollback_summary="Rollback with inverse SQL",
        verification_criteria=["affected rows match expectation"],
    )

    assert readonly["readonly"] is True
    assert "lock_inspect" in diagnostic["allowed_operations"]
    assert write["mode"] == "write_after_approval"
    assert write["approval_id"] == "approval-1"


def test_database_environment_manager_blocks_write_session_in_production(monkeypatch):
    monkeypatch.setenv("POSTGRES_TARGET_ENV", "production")
    profile = build_database_environment_profile("postgresql://user:secret@prod.example.com:5432/app")
    manager = DatabaseEnvironmentManager(profile)

    try:
        manager.write_session(
            approval_id="approval-1",
            sql_hash="abc12345",
            rollback_summary="Rollback with inverse SQL",
            verification_criteria=["affected rows match expectation"],
        )
    except PermissionError as exc:
        assert "production" in str(exc)
    else:
        raise AssertionError("Expected production write session to be blocked")


def test_shell_blocks_database_client_commands(monkeypatch):
    tool = ShellTool()
    tool._whitelist = {"psql", "echo"}
    tool._dangerous = set()

    blocked = tool._run("psql -c 'select 1'")
    allowed = tool._run("echo ok")

    assert blocked.startswith("COMMAND_BLOCKED")
    assert "PostgreSQL domain tools" in blocked
    assert "ok" in allowed


def test_shell_blocks_cwd_outside_workspace(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    monkeypatch.chdir(root)
    tool = ShellTool()
    tool._whitelist = {"pwd"}
    tool._dangerous = set()

    result = tool._run("pwd", cwd=str(outside))

    assert result.startswith("Error:")
    assert "outside workspace" in result


def test_production_environment_allows_dry_run_but_hides_mutating_postgres_tools(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db_env = build_database_environment_profile("postgresql://user:secret@prod.example.com/app")
    db_env["environment_name"] = "production"
    db_env["is_production"] = True
    db_env["allow_write_tools"] = False
    runtime_policy = build_runtime_policy(db_env)
    state = _state(database_environment=db_env, runtime_policy=runtime_policy)

    registry = SkillRegistry()
    registry.register(PostgresDryRunTool())
    registry.register(PostgresExecuteWriteTool())
    tools, _ = registry.get_for_state(state)
    names = {tool.name for tool in tools}

    assert "postgres_dry_run" in names
    assert "postgres_execute_write" not in names


def test_execute_write_tool_blocks_production_before_database_connection(monkeypatch):
    monkeypatch.setenv("POSTGRES_TARGET_ENV", "production")
    sql = "UPDATE orders SET status = 'done' WHERE id = 1"
    output = PostgresExecuteWriteTool()._run(
        sql,
        approval_id="approval-1",
        approved_sql_hash=classify_sql(sql, allow_explain_analyze=True).normalized_sql_hash,
        target_environment="production",
        impact_summary="Update one row",
        rollback_summary="Restore the old value",
    )
    result = loads_result(output)

    assert result is not None
    assert result["result_type"] == "policy_denied"
    assert "production" in result["summary"]


def test_tool_execution_result_includes_environment_summary(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    env_summary = ExecutionEnvironmentManager(_state()).invocation_environment_summary()
    content = dumps_result(
        make_result(
            tool_name="postgres_health_check",
            success=True,
            result_type="health_report",
            summary="ok",
            payload={"checks": {}},
            duration_ms=1,
        )
    )
    msg = ToolMessage(content=content, name="postgres_health_check", tool_call_id="call-env")

    result = _tool_execution_result(msg, datetime.now(timezone.utc), env_summary)

    assert result["payload"]["execution_environment"]["workspace_root"] == str(tmp_path)
    assert "database_environment" in result["payload"]["execution_environment"]
    assert "task_workspace" in result["payload"]["execution_environment"]


def test_tool_invocation_record_receives_environment_and_artifact_refs(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    env_summary = ExecutionEnvironmentManager(_state()).invocation_environment_summary()
    result = {
        "tool_call_id": "call-env",
        "tool_name": "postgres_health_check",
        "success": True,
        "result_type": "health_report",
        "summary": "ok",
        "payload": {"execution_environment": env_summary},
        "duration_ms": 5,
    }
    artifact = {"id": "artifact-1", "payload_ref": "call-env"}
    state = _state(
        tool_invocation_records=[
            {
                "id": "tool-1",
                "call_id": "call-env",
                "tool_name": "postgres_health_check",
                "step_id": "execute",
                "intent_id": None,
                "args_digest": {},
                "policy_decision": {
                    "call_id": "call-env",
                    "tool_name": "postgres_health_check",
                    "decision": "allow",
                    "reason": "allowed",
                    "risk_level": "low",
                    "approval_required": False,
                    "approval_payload": None,
                },
                "approval_id": None,
                "started_at": "now",
                "ended_at": None,
                "status": "pending",
                "duration_ms": None,
                "result_ref": None,
                "observation_ids": [],
                "error_type": None,
                "error_message": None,
            }
        ]
    )

    records = _update_invocation_records(state, [result], [artifact])

    assert records[0]["artifact_ids"] == ["artifact-1"]
    assert records[0]["environment_summary"]["workspace_root"] == str(tmp_path)


def test_file_write_results_map_to_artifact_types():
    assert _artifact_kind_for_result({"tool_name": "file_write", "summary": "Created file: query.sql (10 bytes)"}) == "sql_draft"
    assert _artifact_kind_for_result({"tool_name": "file_write", "summary": "Created file: report.md (10 bytes)"}) == "final_report"
    assert _artifact_kind_for_result({"tool_name": "postgres_explain", "result_type": "explain_plan"}) == "explain_json"


def test_prompt_context_includes_environment_without_password(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("POSTGRES_TARGET_ENV", "production")
    update = ExecutionEnvironmentManager(_state()).bootstrap_state()
    text, _ = build_prompt_context(_state(**update))

    assert "Execution Environment" in text
    assert "production" in text
    assert "safe_host_label" in text
    assert "secret" not in text
    assert "credential_ref" not in text
    assert "POSTGRES_TARGET_URL" not in text
