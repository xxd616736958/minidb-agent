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
    assert "postgres_top_queries" in tasks[0]["expected_tools"]


def test_read_only_analysis_fallback_uses_database_tools():
    intent = {
        "id": "intent-readonly",
        "domain": "postgresql",
        "goal": "检查数据库健康状态",
        "primary_intent": "read_only_analysis",
        "risk_level": "low",
        "assumptions": [],
        "constraints": [],
        "evidence_needed": ["health_check", "connection_info"],
        "suggested_workflow": "read_only_analysis_workflow",
    }

    tasks = validate_and_normalize_plan([], _state(intent, "read_only_analysis_workflow"))

    assert tasks[0]["phase"] == "observe"
    assert tasks[0]["tool_policy"] == "read_only_tools"
    assert "postgres_health_check" in tasks[0]["expected_tools"]
    assert "postgres_connection_check" in tasks[0]["expected_tools"]


def test_table_listing_plan_requires_object_listing_tool():
    intent = {
        "id": "intent-tables",
        "domain": "postgresql",
        "goal": "数据库中有哪些表？",
        "user_language_summary": "数据库中有哪些表？",
        "primary_intent": "read_only_analysis",
        "risk_level": "low",
        "assumptions": [],
        "constraints": [],
        "evidence_needed": [],
        "suggested_workflow": "read_only_analysis_workflow",
    }

    tasks = validate_and_normalize_plan([], _state(intent, "read_only_analysis_workflow"))

    assert tasks[0]["id"] == "list-tables"
    assert tasks[0]["tool_policy"] == "read_only_tools"
    assert "postgres_list_objects" in tasks[0]["expected_tools"]
    assert "schema_summary" in tasks[0]["evidence_required"]


def test_slow_sql_plan_requires_top_queries_even_if_llm_omits_it():
    intent = {
        "id": "intent-slow",
        "domain": "postgresql",
        "goal": "数据库执行过的最慢的十条sql是什么",
        "user_language_summary": "数据库执行过的最慢的十条sql是什么",
        "primary_intent": "performance_diagnosis",
        "risk_level": "low",
        "assumptions": [],
        "constraints": [],
        "evidence_needed": [],
        "suggested_workflow": "performance_diagnosis_workflow",
    }
    raw = [
        {
            "id": "collect-evidence",
            "description": "Collect safe diagnostic evidence",
            "dependencies": [],
            "phase": "observe",
            "operation_type": "diagnostic",
            "risk_level": "low",
            "success_criteria": ["Evidence collected"],
            "expected_tools": ["postgres_connection_check"],
            "tool_policy": "read_only_tools",
        }
    ]

    tasks = validate_and_normalize_plan(raw, _state(intent, "performance_diagnosis_workflow"))

    assert "postgres_top_queries" in tasks[0]["expected_tools"]
    assert "top_queries" in tasks[0]["evidence_required"]
    assert "schema_summary" not in tasks[0]["evidence_required"]
    assert "index_summary" not in tasks[0]["evidence_required"]


def test_top_query_request_overrides_noisy_llm_plan_with_short_readonly_path():
    intent = {
        "id": "intent-top-query",
        "domain": "postgresql",
        "goal": "数据库最需要优化的sql是什么？",
        "user_language_summary": "数据库最需要优化的sql是什么？",
        "primary_intent": "performance_diagnosis",
        "risk_level": "low",
        "assumptions": [],
        "constraints": [],
        "evidence_needed": [],
        "suggested_workflow": "performance_diagnosis_workflow",
    }
    raw = [
        {
            "id": "collect-slow-query-details",
            "description": "Describe an object before ranking slow SQL",
            "dependencies": [],
            "phase": "observe",
            "operation_type": "diagnostic",
            "risk_level": "low",
            "success_criteria": ["Details collected"],
            "expected_tools": ["postgres_object_detail"],
            "tool_policy": "read_only_tools",
        }
    ]

    tasks = validate_and_normalize_plan(raw, _state(intent, "performance_diagnosis_workflow"))

    assert [task["id"] for task in tasks] == ["collect-top-queries", "report-optimization-targets"]
    assert "postgres_top_queries" in tasks[0]["expected_tools"]
    assert "postgres_object_detail" not in tasks[0]["expected_tools"]


def test_schema_change_optimization_is_not_overridden_by_slow_sql_diagnostic_path():
    intent = {
        "id": "intent-optimize",
        "domain": "postgresql",
        "goal": "开始优化这条最需要优化的 SQL，创建索引",
        "user_language_summary": "用户要求开始优化之前诊断出的最需要优化的 SQL",
        "primary_intent": "schema_change",
        "operation_nature": "schema_change",
        "risk_level": "high",
        "requires_approval": True,
        "requires_rollback_plan": True,
        "assumptions": [],
        "constraints": [],
        "evidence_needed": ["current_state"],
        "suggested_workflow": "schema_change_workflow",
    }
    raw = [
        {
            "id": "create-index",
            "description": "Create the approved optimization index",
            "dependencies": [],
            "phase": "execute",
            "operation_type": "schema_change",
            "risk_level": "high",
            "requires_approval": True,
            "requires_rollback_plan": True,
            "success_criteria": ["Index is created"],
            "expected_tools": ["postgres_write"],
            "tool_policy": "write_tools_after_approval",
        }
    ]

    tasks = validate_and_normalize_plan(raw, _state(intent, "schema_change_workflow"))
    ids = [task["id"] for task in tasks]

    assert "collect-top-queries" not in ids
    assert any(task["phase"] == "approve" for task in tasks)
    execute = next(task for task in tasks if task["id"] == "create-index")
    assert execute["tool_policy"] == "write_tools_after_approval"
    assert "postgres_write" in execute["expected_tools"]


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
