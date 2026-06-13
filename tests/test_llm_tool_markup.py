"""Tests for normalizing provider-specific tool-call markup."""

from langchain_core.messages import AIMessage, SystemMessage, ToolMessage

from agent.nodes.llm_node import (
    _deterministic_report_response,
    _latest_top_query_sql,
    _normalize_llm_tool_markup,
    _planned_tool_calls_for_step,
    _sanitize_tool_call_messages,
)
from tools.policy import tool_call_items


def test_normalizes_deepseek_dsml_tool_markup_into_tool_calls():
    response = AIMessage(
        content=(
            '准备查询表。\n'
            '<｜｜DSML｜｜tool_calls> <｜｜DSML｜｜invoke name="postgres_list_objects"> '
            '<｜｜DSML｜｜parameter name="object_type" string="false">table</｜｜DSML｜｜parameter> '
            '<｜｜DSML｜｜parameter name="schema" string="false">public</｜｜DSML｜｜parameter> '
            '</｜｜DSML｜｜invoke> </｜｜DSML｜｜tool_calls>'
        )
    )

    normalized = _normalize_llm_tool_markup(response)

    assert normalized.content == "准备查询表。"
    assert normalized.tool_calls[0]["name"] == "postgres_list_objects"
    assert normalized.tool_calls[0]["args"] == {"object_type": "table", "schema_name": "public"}


def test_strips_dsml_markup_when_native_tool_calls_exist():
    response = AIMessage(
        content=(
            '数据库连接正常。现在查询所有表的信息。\n'
            '<｜｜DSML｜｜tool_calls> <｜｜DSML｜｜invoke name="postgres_list_objects"> '
            '<｜｜DSML｜｜parameter name="object_type" string="false">table</｜｜DSML｜｜parameter> '
            '</｜｜DSML｜｜invoke> </｜｜DSML｜｜tool_calls>'
        ),
        tool_calls=[{"name": "postgres_list_objects", "args": {"object_type": "table"}, "id": "call-1"}],
    )

    normalized = _normalize_llm_tool_markup(response)

    assert normalized.content == "数据库连接正常。现在查询所有表的信息。"
    assert normalized.tool_calls == response.tool_calls
    assert "tool_calls" not in normalized.content


def test_sanitize_strips_tool_calls_when_tool_messages_are_not_immediate():
    ai = AIMessage(
        content="",
        tool_calls=[{"name": "postgres_query_readonly", "args": {"sql": "SELECT 1"}, "id": "call-1"}],
    )
    messages = [
        ai,
        SystemMessage(content="recovery inserted before tool result"),
        ToolMessage(content="ok", name="postgres_query_readonly", tool_call_id="call-1"),
    ]

    cleaned = _sanitize_tool_call_messages(messages)

    assert cleaned[0].tool_calls == []
    assert "cancelled" in cleaned[0].content


def test_sanitize_keeps_tool_calls_with_immediate_tool_messages():
    ai = AIMessage(
        content="",
        tool_calls=[{"name": "postgres_query_readonly", "args": {"sql": "SELECT 1"}, "id": "call-1"}],
    )
    tool = ToolMessage(content="ok", name="postgres_query_readonly", tool_call_id="call-1")

    cleaned = _sanitize_tool_call_messages([ai, tool])

    assert cleaned[0].tool_calls == ai.tool_calls


def test_sanitize_strips_dict_tool_calls_when_order_is_invalid():
    messages = [
        {
            "type": "ai",
            "content": "",
            "tool_calls": [{"name": "postgres_read", "args": {"sql": "SELECT 1"}, "id": "call-1"}],
        },
        {"type": "system", "content": "recovery inserted before tool result"},
        {"type": "tool", "content": "ok", "name": "postgres_read", "tool_call_id": "call-1"},
    ]

    cleaned = _sanitize_tool_call_messages(messages)

    assert cleaned[0].tool_calls == []


def test_sanitize_keeps_dict_tool_calls_with_immediate_dict_tool_message():
    messages = [
        {
            "type": "ai",
            "content": "",
            "tool_calls": [{"name": "postgres_read", "args": {"sql": "SELECT 1"}, "id": "call-1"}],
        },
        {"type": "tool", "content": "ok", "name": "postgres_read", "tool_call_id": "call-1"},
    ]

    cleaned = _sanitize_tool_call_messages(messages)

    assert cleaned[0]["tool_calls"] == messages[0]["tool_calls"]


def test_tool_call_items_normalizes_openai_compatible_function_calls():
    message = {
        "type": "ai",
        "content": "",
        "additional_kwargs": {
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {
                        "name": "postgres_query_readonly",
                        "arguments": "{\"sql\":\"SELECT 1\"}",
                    },
                }
            ]
        },
    }

    calls = tool_call_items(message)

    assert calls == [
        {
            "name": "postgres_query_readonly",
            "args": {"sql": "SELECT 1"},
            "id": "call-1",
        }
    ]


def test_planned_tool_calls_use_overview_tool_for_target_prompt():
    state = {
        "current_intent": {
            "domain": "postgresql",
            "goal": "当前连接的是哪个 PostgreSQL 数据库？请说明环境、数据库名、用户、host、权限模式，并列出当前数据库有哪些 schema 和表。",
            "user_language_summary": "当前连接的是哪个 PostgreSQL 数据库？请说明环境、数据库名、用户、host、权限模式，并列出当前数据库有哪些 schema 和表。",
        },
        "task_stack": [
            {
                "id": "inspect-target-overview",
                "description": "Inspect the configured PostgreSQL target, schemas, and user tables",
                "status": "running",
                "dependencies": [],
                "phase": "observe",
                "tool_policy": "read_only_tools",
                "expected_tools": ["postgres_schema_overview"],
            }
        ],
        "current_step_id": "inspect-target-overview",
        "current_task_index": 0,
    }

    calls = _planned_tool_calls_for_step(state)

    assert [call["name"] for call in calls] == ["postgres_schema_overview"]
    assert calls[0]["args"] == {"include_system": False, "table_limit": 500}


def test_planned_tool_calls_collect_deep_optimization_evidence_from_prior_top_query():
    state = {
        "current_intent": {
            "domain": "postgresql",
            "goal": "基于刚才最需要优化的 SQL，继续分析它的执行计划、相关表结构、索引和统计信息，然后给出优化方案、风险和验证标准。不要执行写操作。",
            "user_language_summary": "基于刚才最需要优化的 SQL，继续分析它的执行计划、相关表结构、索引和统计信息，然后给出优化方案、风险和验证标准。不要执行写操作。",
        },
        "db_observations": [
            {
                "type": "top_queries",
                "payload": {"queries": [{"query_preview": "SELECT COUNT(*) FROM public.big_orders_demo"}]},
            }
        ],
        "task_stack": [
            {
                "id": "collect-optimization-evidence",
                "description": "Collect evidence",
                "status": "running",
                "dependencies": [],
                "phase": "observe",
                "tool_policy": "read_only_tools",
                "expected_tools": ["postgres_explain", "postgres_object_detail"],
            }
        ],
        "current_step_id": "collect-optimization-evidence",
        "current_task_index": 0,
    }

    calls = _planned_tool_calls_for_step(state)

    names = [call["name"] for call in calls]
    assert "postgres_explain" in names
    assert "postgres_object_detail" in names
    explain = next(call for call in calls if call["name"] == "postgres_explain")
    detail = next(call for call in calls if call["name"] == "postgres_object_detail")
    assert explain["args"]["sql"] == "SELECT COUNT(*) FROM public.big_orders_demo"
    assert detail["args"]["schema_name"] == "public"
    assert detail["args"]["object_name"] == "big_orders_demo"


def test_latest_top_query_prefers_select_with_optimization_shape_over_insert():
    state = {
        "db_observations": [
            {
                "type": "top_queries",
                "source_tool": "postgres_top_queries",
                "payload": {
                    "queries": [
                        {
                            "query_preview": "INSERT INTO public.big_orders_demo SELECT generate_series($1, $2)",
                            "total_exec_time": 50000,
                            "mean_exec_time": 50000,
                            "calls": 1,
                        },
                        {
                            "query_preview": "SELECT status, COUNT(*) FROM public.big_orders_demo GROUP BY status ORDER BY COUNT(*) DESC",
                            "total_exec_time": 2000,
                            "mean_exec_time": 200,
                            "calls": 10,
                            "shared_blks_hit": 1000,
                        },
                    ]
                },
            }
        ]
    }

    assert _latest_top_query_sql(state).startswith("SELECT status")


def test_deterministic_report_identifies_top_query_without_model_call():
    state = {
        "task_stack": [
            {
                "id": "report-optimization-targets",
                "description": "Report top query",
                "status": "running",
                "dependencies": ["collect-top-queries"],
                "phase": "report",
                "tool_policy": "no_tools",
            }
        ],
        "current_step_id": "report-optimization-targets",
        "current_task_index": 0,
        "db_observations": [
            {
                "id": "obs-top",
                "step_id": "collect-top-queries",
                "type": "top_queries",
                "source_tool": "postgres_top_queries",
                "summary": "Collected 2 top query row(s).",
                "payload": {
                    "queries": [
                        {
                            "query_preview": "SELECT status, COUNT(*) FROM public.big_orders_demo GROUP BY status",
                            "total_exec_time": 2000,
                            "mean_exec_time": 200,
                            "calls": 10,
                        }
                    ]
                },
                "created_at": "now",
            }
        ],
    }

    response = _deterministic_report_response(state)

    assert response is not None
    assert response.tool_calls == []
    assert "SELECT status" in response.content
    assert "关键证据" in response.content


def test_deterministic_approval_report_includes_sql_and_no_execution_claim():
    state = {
        "database_environment": {"access_mode": "write_after_approval"},
        "task_stack": [
            {
                "id": "request-approval",
                "description": "Request approval",
                "status": "running",
                "dependencies": ["draft-change-sql"],
                "phase": "approve",
                "tool_policy": "no_tools",
            }
        ],
        "current_step_id": "request-approval",
        "current_task_index": 0,
        "db_observations": [
            {
                "id": "obs-classify",
                "step_id": "draft-change-sql",
                "type": "sql_classification",
                "source_tool": "postgres_sql_classify",
                "summary": "SQL classified as schema_change with high risk.",
                "payload": {
                    "sql": "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_big_orders_demo_status ON public.big_orders_demo (status);",
                    "sql_hash": "hash-1",
                    "primary_type": "schema_change",
                    "risk_level": "high",
                },
                "created_at": "now",
            }
        ],
    }

    response = _deterministic_report_response(state)

    assert response is not None
    assert "当前没有执行任何写操作" in response.content
    assert "CREATE INDEX CONCURRENTLY" in response.content
    assert "hash-1" in response.content


def test_planned_tool_calls_execute_approved_change_from_approval_record():
    state = {
        "approval_decisions": [
            {
                "id": "approval-1",
                "step_id": "request-approval",
                "status": "approved",
                "target_environment": "dev",
                "sql_preview": "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_big_orders_demo_status ON public.big_orders_demo (status);",
                "sql_hash": "hash-1",
                "impact_summary": "Create optimization index.",
                "rollback_summary": "DROP INDEX CONCURRENTLY IF EXISTS public.idx_big_orders_demo_status;",
            }
        ],
        "task_stack": [
            {
                "id": "execute-approved-change",
                "description": "Execute approved SQL",
                "status": "running",
                "dependencies": ["request-approval"],
                "phase": "execute",
                "tool_policy": "write_tools_after_approval",
                "expected_tools": ["postgres_execute_write"],
            }
        ],
        "current_step_id": "execute-approved-change",
        "current_task_index": 0,
    }

    calls = _planned_tool_calls_for_step(state)

    assert [call["name"] for call in calls] == ["postgres_execute_write"]
    assert calls[0]["args"]["approval_id"] == "approval-1"
    assert calls[0]["args"]["approved_sql_hash"] == "hash-1"
    assert calls[0]["args"]["sql"].startswith("CREATE INDEX CONCURRENTLY")


def test_planned_tool_calls_for_readonly_error_probe_include_recovery_detail():
    state = {
        "current_intent": {"domain": "postgresql", "goal": "not_exist_column"},
        "task_stack": [
            {
                "id": "probe-readonly-error",
                "description": "Probe error",
                "status": "running",
                "dependencies": [],
                "phase": "observe",
                "tool_policy": "read_only_tools",
                "expected_tools": ["postgres_query_readonly", "postgres_object_detail"],
            }
        ],
        "current_step_id": "probe-readonly-error",
        "current_task_index": 0,
    }

    calls = _planned_tool_calls_for_step(state)

    assert [call["name"] for call in calls] == ["postgres_query_readonly", "postgres_object_detail"]
    assert "not_exist_column" in calls[0]["args"]["sql"]
