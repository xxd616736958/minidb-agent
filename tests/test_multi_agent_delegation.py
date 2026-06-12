"""Tests for controlled multi-agent delegation helpers."""

import pytest

from agent.context import build_prompt_context
from agent.nodes.delegation_planner import delegation_planner
from delegation.manager import DelegationManager, default_agent_roles
from quality.manager import QualityManager
from state_management.migration import StateMigration
from state_management.validator import StateValidator
from tools.builtin.postgres import PostgresExecuteWriteTool, PostgresExplainTool, PostgresQueryReadonlyTool
from tools.catalog import build_tool_spec


pytestmark = pytest.mark.delegation


def _step(step_id="diagnose", **extra):
    step = {
        "id": step_id,
        "description": "Analyze slow orders query and index options",
        "status": "running",
        "dependencies": [],
        "result": None,
        "error": None,
        "phase": "diagnose",
        "operation_type": "diagnostic",
        "risk_level": "low",
        "requires_approval": False,
        "requires_rollback_plan": False,
        "evidence_required": ["explain_plan"],
        "success_criteria": ["Identify bottleneck with evidence"],
        "expected_tools": ["postgres_explain"],
        "tool_policy": "read_only_tools",
    }
    step.update(extra)
    return step


def _state(**extra):
    steps = [_step()]
    state = {
        "messages": [],
        "session_id": "session-1",
        "task_stack": steps,
        "current_task_index": 0,
        "current_step_id": "diagnose",
        "db_task_plan": {
            "id": "plan-1",
            "intent_id": "intent-1",
            "workflow": "performance_diagnosis",
            "summary": "Diagnose slow SQL",
            "status": "running",
            "steps": steps,
            "assumptions": [],
            "constraints": ["read-only"],
            "global_risk_level": "low",
            "requires_user_confirmation": False,
            "created_at": "now",
            "updated_at": "now",
        },
        "current_intent": {
            "id": "intent-1",
            "domain": "postgresql",
            "primary_intent": "performance_diagnosis",
            "goal": "Find why orders query is slow",
            "target_environment": "staging",
            "target_database": "app",
            "target_objects": [{"type": "table", "name": "orders"}],
            "constraints": ["read-only"],
            "risk_level": "low",
        },
        "db_observations": [
            {
                "id": "obs-explain",
                "step_id": "diagnose",
                "type": "explain_plan",
                "source_tool": "postgres_explain",
                "summary": "Seq Scan on orders",
                "payload": {"plan": "Seq Scan"},
                "created_at": "now",
            }
        ],
        "agent_roles": default_agent_roles(),
    }
    state.update(extra)
    return state


def test_default_roles_are_fixed_database_specialists_without_write_tools():
    roles = default_agent_roles()

    assert {role["name"] for role in roles} >= {
        "schema_explorer",
        "performance_analyst",
        "safety_reviewer",
        "migration_planner",
        "report_writer",
    }
    for role in roles:
        assert "postgres_execute_write" not in role["allowed_tools"]
        assert role["permission_mode"] in {"read_only", "proposal_only", "review_only"}


def test_policy_delegates_performance_step_to_performance_analyst():
    decision = DelegationManager(_state()).policy_decision()

    assert decision["decision"] == "delegate"
    assert "performance_analyst" in decision["selected_roles"]
    assert "Performance" in decision["reason"] or "performance" in decision["reason"]


def test_high_risk_step_requires_reviewer_and_migration_planner():
    step = _step(
        "propose-ddl",
        description="Propose ALTER TABLE for orders",
        phase="propose",
        operation_type="schema_change",
        risk_level="high",
        tool_policy="read_only_tools",
    )
    state = _state(
        task_stack=[step],
        current_step_id="propose-ddl",
        db_task_plan={**_state()["db_task_plan"], "steps": [step], "global_risk_level": "high"},
    )

    decision = DelegationManager(state).policy_decision(step)

    assert decision["decision"] == "review_required"
    assert "safety_reviewer" in decision["selected_roles"]
    assert "migration_planner" in decision["selected_roles"]


def test_planning_update_creates_delegated_tasks_and_team_run():
    update = DelegationManager(_state(agent_roles=[])).planning_update()

    assert update["agent_roles"]
    assert update["delegation_policy_decisions"][0]["selected_roles"] == ["performance_analyst"]
    assert update["delegated_tasks"][0]["agent_role"] == "performance_analyst"
    assert update["delegated_tasks"][0]["context_packet"]["target_scope"]["database"] == "app"
    assert update["agent_team_runs"][0]["delegated_task_ids"] == [update["delegated_tasks"][0]["id"]]


def test_graph_delegation_planner_node_returns_state_update():
    update = delegation_planner(_state(agent_roles=[]))

    assert update["agent_roles"]
    assert update["delegation_policy_decisions"]
    assert update["delegated_tasks"]
    assert update["state_integrity_reports"][0]["ok"] is True


def test_role_tool_specs_filter_blocks_write_tools():
    state = _state(
        available_tool_specs=[
            build_tool_spec(PostgresQueryReadonlyTool()),
            build_tool_spec(PostgresExplainTool()),
            build_tool_spec(PostgresExecuteWriteTool()),
        ]
    )

    specs = DelegationManager(state).allowed_tool_specs_for_role("performance_analyst")

    names = {spec["name"] for spec in specs}
    assert "postgres_explain" in names
    assert "postgres_execute_write" not in names


def test_report_writer_receives_no_tools_when_role_allows_none():
    state = _state(
        available_tool_specs=[
            build_tool_spec(PostgresQueryReadonlyTool()),
            build_tool_spec(PostgresExplainTool()),
        ]
    )

    specs = DelegationManager(state).allowed_tool_specs_for_role("report_writer")

    assert specs == []


def test_delegation_result_evaluation_passes_with_evidence():
    manager = DelegationManager(_state())
    decision = manager.policy_decision()
    task = manager.delegated_tasks_for_decision(decision)[0]
    result = manager.result_from_task(
        task,
        evidence_refs=["obs-explain"],
        sql_used=["EXPLAIN SELECT * FROM orders"],
        confidence=0.8,
    )

    evaluation = manager.evaluate_result(result, task)

    assert evaluation["status"] == "passed"
    assert evaluation["safety_compliant"] is True
    assert evaluation["conclusion_supported"] is True


def test_delegation_result_evaluation_fails_on_write_sql_and_missing_evidence():
    manager = DelegationManager(_state(db_observations=[]))
    decision = manager.policy_decision()
    task = manager.delegated_tasks_for_decision(decision)[0]
    result = manager.result_from_task(
        task,
        evidence_refs=[],
        sql_used=["UPDATE orders SET status = 'archived'"],
    )

    evaluation = manager.evaluate_result(result, task)

    assert evaluation["status"] == "failed"
    assert "read_only_sql_only" in evaluation["failed_checks"]
    assert "evidence_available" in evaluation["failed_checks"]


def test_quality_gate_blocks_unreviewed_high_risk_delegation_result():
    high_step = _step("ddl", operation_type="schema_change", risk_level="high", phase="propose")
    state = _state(task_stack=[high_step], current_step_id="ddl")
    manager = DelegationManager(state)
    decision = manager.policy_decision(high_step)
    task = manager.delegated_tasks_for_decision(decision, step=high_step)[0]
    result = manager.result_from_task(task, evidence_refs=["obs-explain"])
    evaluation = manager.evaluate_result(result, task)

    gate = QualityManager({**state, "delegated_tasks": [task]}).delegation_result_gate(result, evaluation, task)

    assert result["requires_human_review"] is True
    assert evaluation["status"] == "needs_review"
    assert gate["status"] == "failed"
    assert "review_status_resolved" in gate["failed_checks"]


def test_migration_and_context_include_delegation_state():
    state = _state(agent_roles=[])
    migration = StateMigration(state).migrate()

    assert migration["delegated_tasks"] == []
    assert migration["delegation_results"] == []

    manager = DelegationManager(_state())
    update = manager.planning_update()
    context, _ = build_prompt_context(
        {
            **_state(),
            **update,
            "context_token_budget": 1200,
        }
    )

    assert "Multi-Agent Delegation" in context
    assert "performance_analyst" in context


def test_state_validator_rejects_delegated_task_with_write_tool():
    manager = DelegationManager(_state())
    decision = manager.policy_decision()
    task = manager.delegated_tasks_for_decision(decision)[0]
    task = {**task, "allowed_tools": [*task["allowed_tools"], "postgres_execute_write"]}

    report = StateValidator({**_state(), "delegated_tasks": [task]}).validate()

    assert report["ok"] is False
    assert any("write-capable tools" in error for error in report["errors"])
