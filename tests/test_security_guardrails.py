"""Tests for safety guardrails and permission controls."""

from __future__ import annotations

from agent.nodes.agent_loop import tool_policy_gate
from execution.environment import build_database_environment_profile, build_runtime_policy
from safety.engine import SecurityPolicyEngine, build_sql_safety_report
from tools.builtin.postgres import PostgresDryRunTool, PostgresExecuteWriteTool, PostgresQueryReadonlyTool
from tools.registry import SkillRegistry, registry


def setup_function():
    registry.clear()


def _step(**extra):
    step = {
        "id": "execute",
        "description": "Execute database change",
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
        "success_criteria": ["affected rows match expectation"],
        "expected_tools": ["postgres_write"],
        "tool_policy": "write_tools_after_approval",
    }
    step.update(extra)
    return step


def _state(**extra):
    db_env = build_database_environment_profile("postgresql://user:secret@dev.example.com/app")
    db_env["environment_name"] = "dev"
    db_env["is_production"] = False
    db_env["allow_write_tools"] = True
    state = {
        "messages": [],
        "task_stack": [_step()],
        "current_task_index": 0,
        "current_step_id": "execute",
        "approval_decisions": [],
        "retrieved_memories": [],
        "database_environment": db_env,
        "runtime_policy": build_runtime_policy(db_env),
    }
    state.update(extra)
    return state


def test_sql_safety_report_classifies_write_and_requires_approval():
    report = build_sql_safety_report("UPDATE orders SET status = 'done' WHERE id = 1")

    assert report["classification"] == "data_change"
    assert report["requires_approval"] is True
    assert report["sql_hash"]
    assert report["requires_rollback_plan"] is True


def test_security_engine_denies_unknown_environment_write_visibility():
    db_env = build_database_environment_profile("postgresql://user:secret@db.example.com/app")
    assert db_env["environment_name"] == "unknown"
    assert db_env["allow_write_tools"] is False

    state = _state(database_environment=db_env, runtime_policy=build_runtime_policy(db_env))
    catalog = SkillRegistry()
    catalog.register(PostgresExecuteWriteTool())
    spec = catalog.get_spec("postgres_execute_write")

    decision = SecurityPolicyEngine(state).evaluate_tool_visibility(spec)

    assert decision["decision"] == "deny"
    assert "unknown" in decision["reasons"][0]


def test_security_engine_requires_sql_hash_bound_approval_for_write():
    registry.register(PostgresExecuteWriteTool())
    sql = "UPDATE orders SET status = 'done' WHERE id = 1"
    tool_call = {
        "name": "postgres_execute_write",
        "args": {
            "sql": sql,
            "approval_id": "approval-1",
            "approved_sql_hash": "wrong-hash",
            "target_environment": "dev",
            "impact_summary": "Update one row",
            "rollback_summary": "Restore old status",
        },
        "id": "call-write",
    }

    decision, sql_report, approval_binding = SecurityPolicyEngine(_state()).evaluate_tool_call(
        tool_call,
        registry.get_spec("postgres_execute_write"),
    )

    assert decision["decision"] == "require_approval"
    assert sql_report is not None
    assert decision["approval_payload"]["sql_hash"] == sql_report["sql_hash"]
    assert approval_binding is None


def test_security_engine_allows_write_with_matching_approval_binding():
    registry.register(PostgresExecuteWriteTool())
    sql = "UPDATE orders SET status = 'done' WHERE id = 1"
    sql_report = build_sql_safety_report(sql, allow_explain_analyze=True)
    state = _state(
        approval_decisions=[
            {
                "id": "approval-1",
                "step_id": "execute",
                "status": "approved",
                "risk_level": "high",
                "target_environment": "dev",
                "sql_preview": sql,
                "sql_hash": sql_report["sql_hash"],
                "impact_summary": "Update one row",
                "rollback_summary": "Restore old status",
                "verification_criteria": ["affected rows match expectation"],
                "user_message": None,
                "created_at": "now",
                "resolved_at": "now",
            }
        ]
    )
    tool_call = {
        "name": "postgres_execute_write",
        "args": {
            "sql": sql,
            "approval_id": "approval-1",
            "approved_sql_hash": sql_report["sql_hash"],
            "target_environment": "dev",
            "impact_summary": "Update one row",
            "rollback_summary": "Restore old status",
        },
        "id": "call-write",
    }

    decision, _, approval_binding = SecurityPolicyEngine(state).evaluate_tool_call(
        tool_call,
        registry.get_spec("postgres_execute_write"),
    )

    assert decision["decision"] == "allow"
    assert approval_binding is not None
    assert approval_binding["sql_hash"] == sql_report["sql_hash"]


def test_security_engine_denies_readonly_tool_with_write_sql():
    registry.register(PostgresQueryReadonlyTool())
    state = _state(
        task_stack=[
            _step(
                id="observe",
                phase="observe",
                operation_type="diagnostic",
                risk_level="low",
                requires_approval=False,
                requires_rollback_plan=False,
                expected_tools=["postgres_read"],
                tool_policy="read_only_tools",
            )
        ],
        current_step_id="observe",
    )
    tool_call = {
        "name": "postgres_query_readonly",
        "args": {"sql": "DELETE FROM orders WHERE id = 1"},
        "id": "call-read",
    }

    decision, sql_report, _ = SecurityPolicyEngine(state).evaluate_tool_call(
        tool_call,
        registry.get_spec("postgres_query_readonly"),
    )

    assert decision["decision"] == "deny"
    assert sql_report["classification"] == "data_change"


def test_security_engine_allows_dry_run_to_analyze_write_sql_without_approval():
    registry.register(PostgresDryRunTool())
    tool_call = {
        "name": "postgres_dry_run",
        "args": {"sql": "UPDATE orders SET status = 'done' WHERE id = 1"},
        "id": "call-dry",
    }

    decision, sql_report, _ = SecurityPolicyEngine(_state()).evaluate_tool_call(
        tool_call,
        registry.get_spec("postgres_dry_run"),
    )

    assert decision["decision"] == "allow"
    assert sql_report["classification"] == "data_change"


def test_tool_policy_gate_records_security_decisions_and_audits():
    from langchain_core.messages import AIMessage

    registry.register(PostgresQueryReadonlyTool())
    msg = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "postgres_query_readonly",
                "args": {"sql": "DELETE FROM orders WHERE id = 1"},
                "id": "call-deny",
            }
        ],
    )
    state = _state(
        messages=[msg],
        task_stack=[
            _step(
                id="observe",
                phase="observe",
                operation_type="diagnostic",
                risk_level="low",
                requires_approval=False,
                requires_rollback_plan=False,
                expected_tools=["postgres_read"],
                tool_policy="read_only_tools",
            )
        ],
        current_step_id="observe",
    )

    update = tool_policy_gate(state)

    assert update["tool_policy_decisions"][0]["decision"] == "deny"
    assert update["security_policy_decisions"][0]["decision"] == "deny"
    assert update["sql_safety_reports"][0]["classification"] == "data_change"
    assert update["safety_audit_records"][0]["event_type"] in {"tool_denied", "sql_denied"}


def test_tool_policy_gate_creates_pending_approval_for_write_call():
    from langchain_core.messages import AIMessage

    registry.register(PostgresExecuteWriteTool())
    sql = "UPDATE orders SET status = 'done' WHERE id = 1"
    msg = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "postgres_execute_write",
                "args": {
                    "sql": sql,
                    "approval_id": "approval-later",
                    "approved_sql_hash": "pending",
                    "target_environment": "dev",
                    "impact_summary": "Update one row",
                    "rollback_summary": "Restore old status",
                },
                "id": "call-approval",
            }
        ],
    )

    update = tool_policy_gate(_state(messages=[msg]))

    assert update["tool_policy_decisions"][0]["decision"] == "require_approval"
    assert update["pending_approval"]["status"] == "pending"
    assert update["pending_approval"]["sql_hash"] == update["sql_safety_reports"][0]["sql_hash"]
    assert update["loop_status"] == "waiting_for_approval"
