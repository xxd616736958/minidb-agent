"""Tests for evaluation, testing, and quality-control helpers."""

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from agent.context import build_prompt_context
from quality.manager import QualityManager
from state_management.migration import StateMigration
from tools.builtin.postgres import PostgresQueryReadonlyTool
from tools.catalog import build_tool_spec


def _step(step_id="observe", **extra):
    step = {
        "id": step_id,
        "description": "Observe database",
        "status": "completed",
        "dependencies": [],
        "result": "ok",
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
    steps = [_step()]
    state = {
        "messages": [HumanMessage(content="诊断 orders 慢查询"), AIMessage(content="诊断完成，证据见 obs-1。")],
        "task_stack": steps,
        "current_task_index": 0,
        "current_step_id": "observe",
        "db_task_plan": {
            "id": "plan-1",
            "intent_id": "intent-1",
            "workflow": "performance_diagnosis_workflow",
            "summary": "Diagnose",
            "status": "completed",
            "steps": steps,
            "assumptions": [],
            "constraints": ["只读"],
            "global_risk_level": "low",
            "requires_user_confirmation": False,
            "created_at": "now",
            "updated_at": "now",
        },
        "current_intent": {
            "id": "intent-1",
            "domain": "postgresql",
            "primary_intent": "performance_diagnosis",
            "target_environment": "staging",
            "target_database": "app",
            "risk_level": "low",
        },
        "db_observations": [
            {
                "id": "obs-1",
                "step_id": "observe",
                "type": "explain_plan",
                "source_tool": "postgres_explain",
                "summary": "Seq Scan on orders",
                "payload": {},
                "created_at": "now",
            }
        ],
        "verification_results": [
            {
                "id": "verify-1",
                "step_id": "observe",
                "status": "passed",
                "criteria_checked": ["done"],
                "evidence_ids": ["obs-1"],
                "summary": "done",
                "created_at": "now",
            }
        ],
        "state_integrity_reports": [
            {
                "ok": True,
                "errors": [],
                "warnings": [],
                "repair_actions": [],
                "created_at": "now",
            }
        ],
    }
    state.update(extra)
    return state


def test_task_completion_gate_passes_read_only_diagnostic_task():
    gate = QualityManager(_state()).task_completion_gate()

    assert gate["gate_type"] == "task_completion"
    assert gate["status"] == "passed"
    assert "evidence_available" in gate["passed_checks"]


def test_task_completion_gate_requires_approval_and_rollback_for_write_task():
    write_step = _step(
        "execute",
        phase="execute",
        operation_type="data_change",
        risk_level="high",
        requires_approval=True,
        requires_rollback_plan=True,
        tool_policy="write_tools_after_approval",
    )
    state = _state(
        task_stack=[write_step],
        db_task_plan={**_state()["db_task_plan"], "steps": [write_step], "global_risk_level": "high"},
        approval_decisions=[],
    )

    gate = QualityManager(state).task_completion_gate()

    assert gate["status"] == "failed"
    assert "approval_recorded" in gate["failed_checks"]
    assert "sql_hash_recorded" in gate["failed_checks"]


def test_tool_contract_gate_checks_postgres_tool_metadata():
    spec = build_tool_spec(PostgresQueryReadonlyTool())

    gate = QualityManager({}).tool_contract_gate(spec)

    assert gate["gate_type"] == "tool_contract"
    assert gate["status"] == "passed"
    assert "args_schema_declared" in gate["passed_checks"]


@pytest.mark.security
def test_safety_regression_gate_blocks_unknown_environment_writes():
    state = _state(
        database_environment={"environment_name": "unknown", "is_production": False},
        runtime_policy={"allow_database_writes": True},
        sql_safety_reports=[
            {
                "sql_hash": "hash-1",
                "normalized_sql_preview": "update orders set status='x'",
                "classification": "data_change",
                "contains_multiple_statements": False,
                "contains_dangerous_constructs": [],
                "target_objects": [],
                "requires_approval": True,
                "requires_rollback_plan": True,
                "requires_backup_check": False,
                "can_run_in_readonly_transaction": False,
                "risk_level": "high",
                "denial_reason": None,
            }
        ],
    )

    gate = QualityManager(state).safety_regression_gate()

    assert gate["status"] == "failed"
    assert "no_unknown_environment_writes" in gate["failed_checks"]
    assert "write_sql_has_approval" in gate["failed_checks"]


def test_run_evaluation_case_checks_state_output_tools_and_evidence():
    case = {
        "id": "eval-slow-query",
        "category": "postgresql_task",
        "user_input": "诊断 orders 慢查询",
        "initial_state": {},
        "expected_state_assertions": [
            {"path": "current_intent.primary_intent", "op": "equals", "value": "performance_diagnosis"},
            {"path": "db_observations", "op": "count_at_least", "value": 1},
        ],
        "expected_output_assertions": [{"op": "contains", "value": "证据"}],
        "forbidden_actions": ["postgres_execute_write"],
        "allowed_tools": ["postgres_explain"],
        "required_evidence": ["obs-1"],
        "tags": ["quick"],
    }

    result = QualityManager(_state()).run_evaluation_case(case)

    assert result["status"] == "passed"
    assert result["scores"]["tool_compliance"] == 1.0
    assert result["evidence_refs"] == ["obs-1"]


def test_replay_case_from_state_captures_messages_and_tool_refs():
    state = _state(
        tool_invocation_records=[
            {
                "id": "tool-1",
                "call_id": "call-1",
                "tool_name": "postgres_explain",
            }
        ],
        context_snapshots=[{"id": "snapshot-1"}],
    )

    replay = QualityManager(state).replay_case_from_state(source="failed_task", expected_recovery="rewrite_sql")

    assert replay["source"] == "failed_task"
    assert replay["tool_invocation_refs"] == ["tool-1"]
    assert replay["expected_recovery"] == "rewrite_sql"
    assert replay["state_snapshot_ref"] == "state.context_snapshots[-1]"


def test_quality_report_summarizes_gates_evaluations_and_risks():
    manager = QualityManager(_state())
    gate = manager.task_completion_gate()
    result = manager.run_evaluation_case(
        {
            "id": "eval-1",
            "category": "postgresql_task",
            "user_input": "diagnose",
            "initial_state": {},
            "expected_state_assertions": [],
            "expected_output_assertions": [],
            "forbidden_actions": [],
            "allowed_tools": [],
            "required_evidence": [],
            "tags": [],
        }
    )

    report = manager.quality_report(
        target_ref="plan-1",
        scope="task",
        gates=[gate, manager.safety_regression_gate()],
        evaluation_results=[result],
    )

    assert report["status"] == "passed"
    assert report["test_summary"]["quality_gates"] == 2
    assert report["evaluation_summary"]["evaluation_results"] == 1


def test_high_risk_change_gate_requires_human_review():
    gate = QualityManager({}).high_risk_change_gate(["safety/engine.py", "README.md"])

    assert gate["gate_type"] == "human_review"
    assert gate["status"] == "failed"
    assert gate["blocking"] is True


def test_migration_and_context_include_quality_state():
    state = _state()
    migration = StateMigration(state).migrate()

    assert migration["quality_gates"] == []
    assert migration["evaluation_cases"] == []
    assert migration["quality_reports"] == []

    manager = QualityManager(state)
    gate = manager.task_completion_gate()
    report = manager.quality_report(target_ref="plan-1", scope="task", gates=[gate], evaluation_results=[])
    context, _ = build_prompt_context(
        {
            **state,
            "quality_gates": [gate],
            "quality_reports": [report],
            "context_token_budget": 500,
        }
    )

    assert "Evaluation Testing and Quality Control" in context
    assert "task_completion" in context
