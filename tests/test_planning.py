"""Tests for PostgreSQL-aware task planning."""

from agent.nodes.task_planner import (
    build_db_task_plan,
    validate_and_normalize_plan,
    task_planner,
)
from langchain_core.messages import HumanMessage


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
        "output_contract": {"task_kind": "health_check"},
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
        "output_contract": {"task_kind": "table_listing"},
        "suggested_workflow": "read_only_analysis_workflow",
    }

    tasks = validate_and_normalize_plan([], _state(intent, "read_only_analysis_workflow"))

    assert tasks[0]["id"] == "list-tables"
    assert tasks[0]["tool_policy"] == "read_only_tools"
    assert "postgres_list_objects" in tasks[0]["expected_tools"]
    assert "schema_summary" in tasks[0]["evidence_required"]


def test_target_overview_prompt_uses_single_overview_tool():
    intent = {
        "id": "intent-overview",
        "domain": "postgresql",
        "goal": "当前连接的是哪个 PostgreSQL 数据库？请说明环境、数据库名、用户、host、权限模式，并列出当前数据库有哪些 schema 和表。",
        "user_language_summary": "当前连接的是哪个 PostgreSQL 数据库？请说明环境、数据库名、用户、host、权限模式，并列出当前数据库有哪些 schema 和表。",
        "primary_intent": "read_only_analysis",
        "risk_level": "low",
        "assumptions": [],
        "constraints": [],
        "evidence_needed": [],
        "output_contract": {"task_kind": "target_overview"},
        "suggested_workflow": "read_only_analysis_workflow",
    }

    tasks = validate_and_normalize_plan([], _state(intent, "read_only_analysis_workflow"))

    assert [task["id"] for task in tasks] == ["inspect-target-overview", "report-target-overview"]
    assert tasks[0]["expected_tools"] == ["postgres_schema_overview"]


def test_health_prompt_uses_health_fast_path():
    intent = {
        "id": "intent-health",
        "domain": "postgresql",
        "goal": "帮我检查当前数据库的健康状态，你自己决定需要检查哪些常见指标。",
        "user_language_summary": "帮我检查当前数据库的健康状态，你自己决定需要检查哪些常见指标。",
        "primary_intent": "read_only_analysis",
        "risk_level": "low",
        "assumptions": [],
        "constraints": [],
        "evidence_needed": [],
        "output_contract": {"task_kind": "health_check"},
        "suggested_workflow": "read_only_analysis_workflow",
    }

    tasks = validate_and_normalize_plan([], _state(intent, "read_only_analysis_workflow"))

    assert [task["id"] for task in tasks] == ["check-database-health", "report-health"]
    assert "postgres_health_check" in tasks[0]["expected_tools"]
    assert "postgres_top_queries" in tasks[0]["expected_tools"]


def test_deep_optimization_prompt_collects_explain_schema_stats_and_advice():
    intent = {
        "id": "intent-deep",
        "domain": "postgresql",
        "goal": "基于刚才最需要优化的 SQL，继续分析它的执行计划、相关表结构、索引和统计信息，然后给出优化方案、风险和验证标准。不要执行写操作。",
        "user_language_summary": "基于刚才最需要优化的 SQL，继续分析它的执行计划、相关表结构、索引和统计信息，然后给出优化方案、风险和验证标准。不要执行写操作。",
        "primary_intent": "performance_diagnosis",
        "risk_level": "low",
        "assumptions": [],
        "constraints": ["不要执行写操作"],
        "evidence_needed": ["top_queries", "execution_plan", "schema_summary", "index_summary"],
        "output_contract": {"task_kind": "deep_optimization_analysis", "read_only_only": True},
        "suggested_workflow": "performance_diagnosis_workflow",
    }

    tasks = validate_and_normalize_plan([], _state(intent, "performance_diagnosis_workflow"))

    assert [task["id"] for task in tasks] == ["collect-optimization-evidence", "report-optimization-plan"]
    assert "postgres_explain" in tasks[0]["expected_tools"]
    assert "postgres_object_detail" in tasks[0]["expected_tools"]
    assert "postgres_index_advisor" in tasks[0]["expected_tools"]


def test_change_sql_prompt_drafts_and_requests_approval_without_execute_step():
    intent = {
        "id": "intent-change-sql",
        "domain": "postgresql",
        "goal": "请为刚才的优化方案生成可以执行的变更 SQL，并说明影响、回滚方案和验证步骤；如果需要执行，先让我审批。",
        "user_language_summary": "请为刚才的优化方案生成可以执行的变更 SQL，并说明影响、回滚方案和验证步骤；如果需要执行，先让我审批。",
        "primary_intent": "schema_change",
        "operation_nature": "schema_change",
        "risk_level": "high",
        "requires_approval": True,
        "requires_rollback_plan": True,
        "assumptions": [],
        "constraints": [],
        "evidence_needed": [],
        "output_contract": {"task_kind": "change_sql_draft"},
        "suggested_workflow": "schema_change_workflow",
    }

    tasks = validate_and_normalize_plan([], _state(intent, "schema_change_workflow"))

    assert [task["id"] for task in tasks] == ["draft-change-sql", "request-approval"]
    assert tasks[0]["tool_policy"] == "read_only_tools"
    assert tasks[1]["tool_policy"] == "no_tools"
    assert all(task["phase"] != "execute" for task in tasks)


def test_readonly_error_probe_prompt_captures_error_and_recovers_with_schema():
    intent = {
        "id": "intent-error-probe",
        "domain": "postgresql",
        "goal": "故意执行一个只读诊断：查询 public.big_orders_demo 的不存在字段 not_exist_column，看看你如何处理错误并继续给出可用结论。",
        "user_language_summary": "故意执行一个只读诊断：查询 public.big_orders_demo 的不存在字段 not_exist_column，看看你如何处理错误并继续给出可用结论。",
        "primary_intent": "read_only_analysis",
        "risk_level": "low",
        "assumptions": [],
        "constraints": [],
        "evidence_needed": [],
        "output_contract": {"task_kind": "readonly_error_probe"},
        "suggested_workflow": "read_only_analysis_workflow",
    }

    tasks = validate_and_normalize_plan([], _state(intent, "read_only_analysis_workflow"))

    assert [task["id"] for task in tasks] == ["probe-readonly-error", "report-readonly-error"]
    assert "postgres_query_readonly" in tasks[0]["expected_tools"]
    assert "postgres_object_detail" in tasks[0]["expected_tools"]


def test_task_planner_skips_model_for_known_database_prompt():
    intent = {
        "id": "intent-overview",
        "domain": "postgresql",
        "goal": "当前连接的是哪个 PostgreSQL 数据库？请说明环境、数据库名、用户、host、权限模式，并列出当前数据库有哪些 schema 和表。",
        "user_language_summary": "当前连接的是哪个 PostgreSQL 数据库？请说明环境、数据库名、用户、host、权限模式，并列出当前数据库有哪些 schema 和表。",
        "primary_intent": "read_only_analysis",
        "risk_level": "low",
        "assumptions": [],
        "constraints": [],
        "evidence_needed": [],
        "output_contract": {"task_kind": "target_overview"},
        "suggested_workflow": "read_only_analysis_workflow",
    }
    state = {
        **_state(intent, "read_only_analysis_workflow"),
        "messages": [HumanMessage(content=intent["goal"])],
        "task_stack": [],
    }

    result = task_planner(state)

    assert result["planning_strategy"]["type"] == "deterministic_fast_path"
    assert result["planning_strategy"]["model_call_skipped"] is True
    assert result["task_stack"][0]["id"] == "inspect-target-overview"


def test_task_planner_skips_model_for_exact_optimization_target_prompt():
    intent = {
        "id": "intent-top-exact",
        "domain": "postgresql",
        "goal": "找出当前数据库最需要优化的一条 SQL，说明你为什么选择它，并给出关键证据。",
        "user_language_summary": "找出当前数据库最需要优化的一条 SQL，说明你为什么选择它，并给出关键证据。",
        "primary_intent": "performance_diagnosis",
        "risk_level": "low",
        "assumptions": [],
        "constraints": [],
        "evidence_needed": ["top_queries", "active_queries", "connection_info"],
        "output_contract": {"task_kind": "optimization_target"},
        "suggested_workflow": "performance_diagnosis_workflow",
    }
    state = {
        **_state(intent, "performance_diagnosis_workflow"),
        "messages": [HumanMessage(content=intent["goal"])],
        "task_stack": [],
    }

    result = task_planner(state)

    assert result["planning_strategy"]["type"] == "deterministic_fast_path"
    assert result["planning_strategy"]["model_call_skipped"] is True
    assert result["planning_strategy"]["reason"] == "top_query"
    assert [task["id"] for task in result["task_stack"]] == ["collect-top-queries", "report-optimization-targets"]


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
        "evidence_needed": ["top_queries"],
        "output_contract": {"task_kind": "optimization_target"},
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
        "evidence_needed": ["top_queries"],
        "output_contract": {"task_kind": "optimization_target"},
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


def test_planner_does_not_route_optimization_from_raw_phrase_without_structured_intent():
    intent = {
        "id": "intent-unstructured",
        "domain": "postgresql",
        "goal": "数据库最需要优化的sql是什么？",
        "user_language_summary": "数据库最需要优化的sql是什么？",
        "primary_intent": "unknown_or_mixed",
        "risk_level": "unknown",
        "assumptions": [],
        "constraints": [],
        "evidence_needed": [],
        "suggested_workflow": "unknown_or_mixed_workflow",
    }

    tasks = validate_and_normalize_plan([], _state(intent, "unknown_or_mixed_workflow"))

    assert [task["id"] for task in tasks] != ["collect-top-queries", "report-optimization-targets"]
    assert tasks[0]["id"] == "understand-request"


def test_performance_optimization_plan_runs_full_approval_execution_flow():
    intent = {
        "id": "intent-optimize-flow",
        "domain": "postgresql",
        "goal": "请帮我优化数据库中最需要优化的一条慢sql",
        "user_language_summary": "请帮我优化数据库中最需要优化的一条慢sql",
        "primary_intent": "performance_optimization",
        "operation_nature": "schema_change",
        "risk_level": "high",
        "requires_approval": True,
        "requires_rollback_plan": True,
        "assumptions": [],
        "constraints": [],
        "evidence_needed": ["top_queries", "execution_plan", "schema_summary", "index_summary", "sql_classification"],
        "output_contract": {"task_kind": "optimization_execution"},
        "suggested_workflow": "performance_optimization_workflow",
    }

    tasks = validate_and_normalize_plan([], _state(intent, "performance_optimization_workflow"))

    assert [task["id"] for task in tasks] == [
        "collect-top-queries",
        "collect-optimization-evidence",
        "report-optimization-plan",
        "draft-change-sql",
        "request-approval",
        "execute-approved-change",
        "verify-optimization",
        "report-optimization-result",
    ]
    execute = next(task for task in tasks if task["id"] == "execute-approved-change")
    assert execute["tool_policy"] == "write_tools_after_approval"
    assert execute["dependencies"] == ["request-approval"]


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
