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

