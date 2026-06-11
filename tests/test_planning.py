"""Tests for PostgreSQL-aware task planning."""

from agent.nodes.task_planner import (
    build_db_task_plan,
    validate_and_normalize_plan,
)


def _state(intent, workflow="data_change_workflow"):
    return {
        "current_intent": intent,
        "selected_workflow": workflow,
        "confirmed_context": {},
    }


def test_data_change_plan_adds_approval_and_rollback():
    intent = {
        "id": "intent-1",
        "domain": "postgresql",
        "goal": "Clean old orders data",
        "primary_intent": "data_change",
        "risk_level": "high",
        "requires_approval": True,
        "requires_rollback_plan": True,
        "assumptions": [],
        "constraints": [],
        "suggested_workflow": "data_change_workflow",
    }
    raw = [
        {
            "id": "delete-orders",
            "description": "Delete old orders rows",
            "dependencies": [],
            "phase": "execute",
            "operation_type": "data_change",
            "risk_level": "medium",
            "success_criteria": ["Rows are deleted"],
        }
    ]

    tasks = validate_and_normalize_plan(raw, _state(intent))

    approval_steps = [task for task in tasks if task["phase"] == "approve"]
    execute_steps = [task for task in tasks if task["phase"] == "execute"]
    assert approval_steps
    assert execute_steps[0]["requires_approval"] is True
    assert execute_steps[0]["requires_rollback_plan"] is True
    assert execute_steps[0]["tool_policy"] == "write_tools_after_approval"
    assert approval_steps[0]["id"] in execute_steps[0]["dependencies"]


def test_read_only_constraint_skips_write_step():
    intent = {
        "id": "intent-2",
        "domain": "postgresql",
        "goal": "Analyze only",
        "primary_intent": "read_only_analysis",
        "risk_level": "low",
        "assumptions": [],
        "constraints": ["只读，不要执行变更"],
        "suggested_workflow": "read_only_analysis_workflow",
    }
    raw = [
        {
            "id": "bad-write",
            "description": "Update a row",
            "dependencies": [],
            "phase": "execute",
            "operation_type": "data_change",
            "risk_level": "high",
            "success_criteria": ["Row updated"],
        }
    ]

    tasks = validate_and_normalize_plan(raw, _state(intent, "read_only_analysis_workflow"))

    assert tasks[0]["status"] == "skipped"
    assert "read-only" in tasks[0]["error"]


def test_performance_fallback_starts_with_read_only_observe():
    intent = {
        "id": "intent-3",
        "domain": "postgresql",
        "goal": "Diagnose slow orders query",
        "primary_intent": "performance_diagnosis",
        "risk_level": "low",
        "assumptions": [],
        "constraints": [],
        "evidence_needed": ["execution_plan"],
        "suggested_workflow": "performance_diagnosis_workflow",
    }

    tasks = validate_and_normalize_plan([], _state(intent, "performance_diagnosis_workflow"))

    assert tasks[0]["phase"] == "observe"
    assert tasks[0]["tool_policy"] == "read_only_tools"
    assert "execution_plan" in tasks[0]["evidence_required"]


def test_db_task_plan_global_risk_and_confirmation():
    intent = {
        "id": "intent-4",
        "domain": "postgresql",
        "goal": "Alter table",
        "primary_intent": "schema_change",
        "risk_level": "high",
        "assumptions": [],
        "constraints": [],
        "suggested_workflow": "schema_change_workflow",
    }
    tasks = validate_and_normalize_plan([], _state(intent, "schema_change_workflow"))
    plan = build_db_task_plan(tasks, _state(intent, "schema_change_workflow"))

    assert plan["global_risk_level"] == "high"
    assert plan["requires_user_confirmation"] is True
    assert plan["workflow"] == "schema_change_workflow"

