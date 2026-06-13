"""Tests for plan-step driven Agent Loop nodes."""

from langchain_core.messages import AIMessage, ToolMessage

from agent.nodes.agent_loop import (
    normalize_observation,
    step_scheduler,
    tool_policy_gate,
    verify_step,
)


def _step(step_id, status="pending", deps=None, **extra):
    return {
        "id": step_id,
        "description": f"Step {step_id}",
        "status": status,
        "dependencies": deps or [],
        "result": None,
        "error": None,
        "phase": extra.get("phase", "observe"),
        "operation_type": extra.get("operation_type", "diagnostic"),
        "risk_level": extra.get("risk_level", "low"),
        "requires_approval": extra.get("requires_approval", False),
        "requires_rollback_plan": extra.get("requires_rollback_plan", False),
        "evidence_required": extra.get("evidence_required", []),
        "success_criteria": extra.get("success_criteria", ["done"]),
        "expected_tools": extra.get("expected_tools", []),
        "tool_policy": extra.get("tool_policy", "read_only_tools"),
    }


def _state(steps, **extra):
    return {
        "messages": [],
        "task_stack": steps,
        "current_task_index": 0,
        "db_task_plan": {
            "id": "plan-1",
            "intent_id": "intent-1",
            "workflow": "test",
            "summary": "test",
            "status": "draft",
            "steps": steps,
            "assumptions": [],
            "constraints": [],
            "global_risk_level": "low",
            "requires_user_confirmation": False,
            "created_at": "now",
            "updated_at": "now",
        },
        **extra,
    }


def test_step_scheduler_selects_dependency_ready_step():
    steps = [
        _step("observe", status="completed"),
        _step("diagnose", deps=["observe"]),
    ]

    result = step_scheduler(_state(steps))

    assert result["current_step_id"] == "diagnose"
    assert result["task_stack"][1]["status"] == "running"
    assert result["current_task_index"] == 1


def test_step_scheduler_completes_when_all_steps_done():
    steps = [_step("observe", status="completed")]

    result = step_scheduler(_state(steps))

    assert result["loop_status"] == "completed"
    assert result["current_step_id"] is None


def test_tool_policy_gate_blocks_write_sql_in_read_only_step():
    steps = [_step("observe", status="running", tool_policy="read_only_tools")]
    msg = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "postgres_execute",
                "args": {"sql": "DELETE FROM orders"},
                "id": "call-1",
            }
        ],
    )

    result = tool_policy_gate(
        _state(
            steps,
            messages=[msg],
            current_step_id="observe",
        )
    )

    assert result["policy_violation"]["step_id"] == "observe"
    assert "read-only" in result["policy_violation"]["message"]
    assert result["messages"][-1].content.startswith("Blocked tool call")


def test_tool_policy_gate_downgrades_report_tool_call_to_report_instruction():
    steps = [_step("report", status="running", phase="report", tool_policy="no_tools")]
    msg = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "postgres_query_readonly",
                "args": {"sql": "SELECT 1"},
                "id": "call-report",
            }
        ],
    )

    result = tool_policy_gate(
        _state(
            steps,
            messages=[msg],
            current_step_id="report",
        )
    )

    assert result["loop_status"] == "running"
    assert result["policy_violation"] is None
    assert result["messages"][-1].content.startswith("The current report step does not allow")


def test_tool_policy_gate_downgrades_propose_tool_call_to_step_instruction():
    steps = [_step("propose", status="running", phase="propose", tool_policy="no_tools")]
    msg = AIMessage(
        content="影响：会刷新统计信息；验证：检查 last_analyze。",
        tool_calls=[
            {
                "name": "postgres_list_objects",
                "args": {"object_type": "table", "schema_name": "public"},
                "id": "call-propose",
            }
        ],
    )

    result = tool_policy_gate(
        _state(
            steps,
            messages=[msg],
            current_step_id="propose",
        )
    )

    assert result["loop_status"] == "running"
    assert result["policy_violation"] is None
    assert msg.tool_calls == []
    assert "current propose step does not allow" in result["messages"][-1].content


def test_normalize_observation_from_tool_message():
    steps = [_step("observe", status="running")]
    msg = ToolMessage(
        content="EXPLAIN SELECT * FROM orders",
        name="postgres_read",
        tool_call_id="call-1",
    )

    result = normalize_observation(
        _state(steps, messages=[msg], current_step_id="observe")
    )

    assert result["db_observations"][0]["type"] == "explain_plan"
    assert result["db_observations"][0]["step_id"] == "observe"


def test_verify_step_marks_running_step_completed():
    steps = [_step("observe", status="running")]
    observation = {
        "id": "obs-1",
        "step_id": "observe",
        "type": "query_result",
        "source_tool": "postgres_read",
        "summary": "ok",
        "payload": {},
        "created_at": "now",
    }

    result = verify_step(
        _state(
            steps,
            current_step_id="observe",
            db_observations=[observation],
        )
    )

    assert result["task_stack"][0]["status"] == "completed"
    assert result["verification_results"][0]["status"] == "passed"


def test_verify_step_waits_for_required_table_listing_evidence():
    steps = [
        _step(
            "list-tables",
            status="running",
            expected_tools=["postgres_list_objects"],
            evidence_required=["schema_summary"],
            success_criteria=["User table list is collected"],
        )
    ]
    observation = {
        "id": "obs-1",
        "step_id": "list-tables",
        "type": "connection_status",
        "source_tool": "postgres_connection_check",
        "summary": "PostgreSQL connection is available.",
        "payload": {"success": True},
        "created_at": "now",
    }

    result = verify_step(
        _state(
            steps,
            current_step_id="list-tables",
            db_observations=[observation],
        )
    )

    assert result["loop_status"] == "running"
    assert "task_stack" not in result
    assert "verification_results" not in result


def test_verify_step_completes_with_semantic_postgres_evidence_after_repair():
    steps = [
        _step(
            "collect-execution-plan",
            status="running",
            phase="observe",
            operation_type="diagnostic",
            expected_tools=["postgres_read"],
            evidence_required=[
                "execution_plan",
                "schema_summary",
                "index_summary",
                "row_count_or_statistics",
            ],
            success_criteria=["Collect execution plan and object statistics"],
        )
    ]
    observations = [
        {
            "id": "obs-explain",
            "step_id": "collect-execution-plan",
            "type": "explain_plan",
            "source_tool": "postgres_explain",
            "summary": "Plan root: Sort, cost=682650.85.",
            "payload": {"success": True, "plan": {"root_node_type": "Sort"}},
            "created_at": "now",
        },
        {
            "id": "obs-object",
            "step_id": "collect-execution-plan",
            "type": "object_detail",
            "source_tool": "postgres_object_detail",
            "summary": "Collected details for public.big_orders_demo.",
            "payload": {"success": True, "indexes": [{"name": "idx_big_orders_perf"}]},
            "created_at": "now",
        },
        {
            "id": "obs-error",
            "step_id": "collect-execution-plan",
            "type": "sql_error",
            "source_tool": "postgres_query_readonly",
            "summary": 'column "tablename" does not exist',
            "payload": {"success": False, "sqlstate": "42703"},
            "created_at": "now",
        },
        {
            "id": "obs-stats",
            "step_id": "collect-execution-plan",
            "type": "query_result",
            "source_tool": "postgres_query_readonly",
            "summary": "Read-only query returned 1 row(s).",
            "payload": {"success": True, "rows": [{"n_live_tup": 5499937}]},
            "created_at": "now",
        },
    ]
    failed = {
        "tool_call_id": "call-bad",
        "tool_name": "postgres_query_readonly",
        "success": False,
        "result_type": "sql_error",
        "summary": 'column "tablename" does not exist',
        "sqlstate": "42703",
    }

    result = verify_step(
        _state(
            steps,
            current_step_id="collect-execution-plan",
            db_observations=observations,
            tool_execution_results=[failed],
            tool_invocation_records=[{"call_id": "call-bad", "step_id": "collect-execution-plan"}],
        )
    )

    assert result["task_stack"][0]["status"] == "completed"
    assert result["verification_results"][0]["status"] == "passed"


def test_verify_step_maps_chinese_evidence_labels_to_structured_observations():
    steps = [
        _step(
            "collect-top-sql",
            status="running",
            phase="observe",
            operation_type="diagnostic",
            expected_tools=["postgres_read"],
            evidence_required=["pg_stat_statements 查询结果", "表结构", "索引列表", "行数估算", "EXPLAIN ANALYZE BUFFERS 输出"],
            success_criteria=["Collect slow SQL evidence"],
        )
    ]
    observations = [
        {
            "id": "obs-top",
            "step_id": "collect-top-sql",
            "type": "top_queries",
            "source_tool": "postgres_top_queries",
            "summary": "Collected 10 top query row(s).",
            "payload": {"success": True, "queries": [{"query_preview": "SELECT 1"}]},
            "created_at": "now",
        },
        {
            "id": "obs-object",
            "step_id": "collect-top-sql",
            "type": "object_detail",
            "source_tool": "postgres_object_detail",
            "summary": "Collected details for public.big_orders_demo.",
            "payload": {"success": True, "indexes": [{"name": "idx_big_orders_perf"}]},
            "created_at": "now",
        },
        {
            "id": "obs-stats",
            "step_id": "collect-top-sql",
            "type": "query_result",
            "source_tool": "postgres_query_readonly",
            "summary": "Read-only query returned 1 row(s).",
            "payload": {"success": True, "rows": [{"n_live_tup": 5499937}]},
            "created_at": "now",
        },
        {
            "id": "obs-explain",
            "step_id": "collect-top-sql",
            "type": "explain_plan",
            "source_tool": "postgres_explain",
            "summary": "Plan root: Sort, cost=682650.85.",
            "payload": {"success": True, "plan": {"root_node_type": "Sort"}},
            "created_at": "now",
        },
    ]

    result = verify_step(
        _state(
            steps,
            current_step_id="collect-top-sql",
            db_observations=observations,
        )
    )

    assert result["task_stack"][0]["status"] == "completed"
    assert result["verification_results"][0]["status"] == "passed"


def test_verify_diagnose_step_can_complete_from_prior_evidence_and_assistant_text():
    steps = [
        _step("collect-top-queries", status="completed"),
        _step(
            "diagnose-worst-query",
            status="running",
            deps=["collect-top-queries"],
            phase="diagnose",
            operation_type="diagnostic",
            expected_tools=[],
            evidence_required=["execution_plan", "index_summary", "row_count_or_statistics"],
            success_criteria=["Identify bottleneck and recommendation"],
        ),
    ]
    observations = [
        {
            "id": "obs-explain",
            "step_id": "collect-top-queries",
            "type": "explain_plan",
            "source_tool": "postgres_explain",
            "summary": "Plan root: Sort, cost=682650.85.",
            "payload": {"success": True, "plan": {"root_node_type": "Sort"}},
            "created_at": "now",
        },
        {
            "id": "obs-object",
            "step_id": "collect-top-queries",
            "type": "object_detail",
            "source_tool": "postgres_object_detail",
            "summary": "Collected details for public.big_orders_demo.",
            "payload": {"success": True, "indexes": [{"name": "idx_big_orders_perf"}]},
            "created_at": "now",
        },
        {
            "id": "obs-stats",
            "step_id": "collect-top-queries",
            "type": "query_result",
            "source_tool": "postgres_query_readonly",
            "summary": "Read-only query returned 1 row(s).",
            "payload": {"success": True, "rows": [{"n_live_tup": 5499937}]},
            "created_at": "now",
        },
    ]

    result = verify_step(
        _state(
            steps,
            current_task_index=1,
            current_step_id="diagnose-worst-query",
            db_observations=observations,
            messages=[AIMessage(content="瓶颈是聚合排序和临时文件写入，建议评估覆盖索引或预聚合。")],
        )
    )

    assert result["task_stack"][1]["status"] == "completed"
    assert result["verification_results"][0]["status"] == "passed"


def test_verify_step_lets_llm_repair_first_readonly_sql_error():
    steps = [
        _step(
            "diagnose",
            status="running",
            phase="diagnose",
            operation_type="diagnostic",
            tool_policy="read_only_tools",
            expected_tools=["postgres_query_readonly"],
        )
    ]
    failed = {
        "tool_call_id": "call-bad",
        "tool_name": "postgres_query_readonly",
        "success": False,
        "result_type": "sql_error",
        "summary": 'column "tablename" does not exist',
        "sqlstate": "42703",
    }

    result = verify_step(
        _state(
            steps,
            current_step_id="diagnose",
            tool_execution_results=[failed],
            tool_invocation_records=[{"call_id": "call-bad", "step_id": "diagnose"}],
        )
    )

    assert result["loop_status"] == "running"
    assert "task_stack" not in result
    assert "Do not repeat the same failing SQL" in result["messages"][0].content


def test_verify_step_blocks_repeated_readonly_sql_error_without_evidence():
    steps = [
        _step(
            "diagnose",
            status="running",
            phase="diagnose",
            operation_type="diagnostic",
            tool_policy="read_only_tools",
            expected_tools=["postgres_query_readonly"],
        )
    ]
    failed = {
        "tool_call_id": "call-bad",
        "tool_name": "postgres_query_readonly",
        "success": False,
        "result_type": "sql_error",
        "summary": 'column "tablename" does not exist',
        "sqlstate": "42703",
    }
    observation = {
        "id": "obs-failed",
        "step_id": "diagnose",
        "type": "sql_error",
        "source_tool": "postgres_query_readonly",
        "summary": 'column "tablename" does not exist',
        "payload": {"success": False, "sqlstate": "42703"},
        "created_at": "now",
    }

    result = verify_step(
        _state(
            steps,
            current_step_id="diagnose",
            db_observations=[observation],
            tool_execution_results=[failed],
            tool_invocation_records=[{"call_id": "call-bad", "step_id": "diagnose"}],
        )
    )

    assert result["loop_status"] == "blocked"
    assert result["task_stack"][0]["status"] == "failed"


def test_verify_report_step_waits_for_assistant_content():
    steps = [
        _step(
            "report-results",
            status="running",
            phase="report",
            tool_policy="no_tools",
            success_criteria=["Answer includes findings"],
        )
    ]

    result = verify_step(
        _state(
            steps,
            current_step_id="report-results",
            messages=[],
        )
    )

    assert result["loop_status"] == "running"
    assert "task_stack" not in result


def test_verify_report_step_completes_with_assistant_content():
    steps = [
        _step(
            "report-results",
            status="running",
            phase="report",
            tool_policy="no_tools",
            success_criteria=["Answer includes findings"],
        )
    ]

    result = verify_step(
        _state(
            steps,
            current_step_id="report-results",
            messages=[AIMessage(content="最需要优化的是 SELECT COUNT(*) FROM big_orders_demo。")],
        )
    )

    assert result["task_stack"][0]["status"] == "completed"
    assert "SELECT COUNT(*)" in result["task_stack"][0]["result"]
