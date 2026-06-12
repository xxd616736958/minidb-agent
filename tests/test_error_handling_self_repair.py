"""Tests for structured error handling and self-repair."""

from langchain_core.messages import ToolMessage

from agent.context import build_prompt_context
from agent.nodes.agent_loop import verify_step
from agent.nodes.error_handler import error_handler
from agent.nodes.tool_executor import _tool_execution_result
from error_handling.classifier import ErrorClassifier, classify_text
from error_handling.recovery import RecoveryEngine
from state_management.migration import StateMigration
from state_management.validator import StateValidator


def _step(step_id="observe", **extra):
    step = {
        "id": step_id,
        "description": "Observe database",
        "status": "running",
        "dependencies": [],
        "result": None,
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
        "messages": [],
        "task_stack": steps,
        "current_task_index": 0,
        "current_step_id": "observe",
        "db_task_plan": {
            "id": "plan-1",
            "intent_id": "intent-1",
            "workflow": "diagnosis",
            "summary": "Diagnose",
            "status": "running",
            "steps": steps,
            "assumptions": [],
            "constraints": [],
            "global_risk_level": "low",
            "requires_user_confirmation": False,
            "created_at": "now",
            "updated_at": "now",
        },
        "current_intent": {
            "id": "intent-1",
            "domain": "postgresql",
            "target_environment": "staging",
            "target_database": "app",
            "risk_level": "low",
        },
        "error": None,
        "retry_count": 0,
        "max_retries": 3,
    }
    state.update(extra)
    return state


def test_classify_postgresql_error_texts():
    assert classify_text("syntax error at or near FROM") == "syntax_error"
    assert classify_text("canceling statement due to statement timeout") == "statement_timeout"
    assert classify_text("could not obtain lock on relation orders") == "lock_timeout"
    assert classify_text("permission denied for table orders") == "permission_denied"


def test_tool_result_becomes_error_record_with_sqlstate():
    msg = ToolMessage(
        content='{"tool_name":"postgres_query_readonly","success":false,"result_type":"sql_error","summary":"syntax error","payload":{},"sqlstate":"42601","duration_ms":1}',
        name="postgres_query_readonly",
        tool_call_id="call-sql",
    )
    result = _tool_execution_result(msg, __import__("datetime").datetime.now(__import__("datetime").timezone.utc))

    record = ErrorClassifier(_state()).from_tool_result(result)

    assert record["source"] == "postgresql"
    assert record["error_type"] == "syntax_error"
    assert record["tool_name"] == "postgres_query_readonly"
    assert record["tool_call_id"] == "call-sql"


def test_recovery_decision_rewrites_sql_and_requires_new_approval_for_sql_hash():
    error = ErrorClassifier(_state()).record(
        message="syntax error",
        error_type="syntax_error",
        sql_hash="abc123",
        tool_name="postgres_execute_write",
    )

    update = RecoveryEngine(_state()).update_for_error(error)

    assert update["recovery_decisions"][0]["action"] == "rewrite_sql"
    assert update["recovery_decisions"][0]["requires_new_approval"] is True
    assert update["retry_budgets"][0]["attempts"] == 1


def test_recovery_never_retries_policy_denied():
    error = ErrorClassifier(_state()).record(
        message="Blocked by safety policy",
        error_type="policy_denied",
        source="safety_policy",
    )

    update = RecoveryEngine(_state()).update_for_error(error)

    assert update["recovery_decisions"][0]["action"] == "abort_safely"
    assert update["retry_budgets"][0]["max_attempts"] == 0
    assert update["retry_budgets"][0]["exhausted"] is True


def test_state_integrity_error_generates_state_repair_actions():
    state = _state(current_step_id="missing-step")
    report = StateValidator(state).validate()
    error = ErrorClassifier(state).from_integrity_report(report)

    update = RecoveryEngine({**state, "state_integrity_reports": [report]}).update_for_error(error)

    assert update["recovery_decisions"][0]["action"] == "repair_state"
    assert update["state_repair_actions"]


def test_error_handler_outputs_structured_recovery_and_collaboration_event():
    update = error_handler(_state(error="Tool schema invalid: missing sql argument"))

    assert update["error"] is None
    assert update["error_records"][0]["error_type"] == "tool_schema_error"
    assert update["recovery_decisions"][0]["action"] == "auto_retry"
    assert update["active_recovery_decision"]["next_node"] == "llm_reason"
    assert any(event["event_type"] == "retry_scheduled" for event in update["collaboration_events"])


def test_error_handler_generates_error_report_for_permission_error():
    update = error_handler(_state(error="permission denied for table orders"))

    assert update["error_records"][0]["error_type"] == "permission_denied"
    assert update["recovery_decisions"][0]["action"] == "ask_user"
    assert update["error_reports"][0]["status"] == "failed"
    assert update["loop_status"] == "blocked"


def test_verify_step_blocks_failed_tool_result_and_creates_recovery_decision():
    failed_result = {
        "tool_call_id": "call-1",
        "tool_name": "postgres_query_readonly",
        "success": False,
        "result_type": "sql_error",
        "summary": "syntax error at or near FROM",
        "payload": {},
        "row_count": None,
        "affected_rows": None,
        "sqlstate": "42601",
        "duration_ms": 1,
        "truncated": False,
        "sensitive_fields_masked": [],
    }

    update = verify_step(_state(tool_execution_results=[failed_result]))

    assert update["task_stack"][0]["status"] == "failed"
    assert update["error_records"][0]["error_type"] == "syntax_error"
    assert update["recovery_decisions"][0]["action"] == "rewrite_sql"


def test_migration_and_context_include_error_recovery_state():
    state = _state()
    migration = StateMigration(state).migrate()
    assert migration["error_records"] == []
    assert migration["recovery_decisions"] == []
    assert migration["retry_budgets"] == []

    error = ErrorClassifier(state).record(message="statement timeout", error_type="statement_timeout")
    recovery = RecoveryEngine(state).update_for_error(error)
    context, _ = build_prompt_context({**state, **recovery, "context_token_budget": 400})

    assert "Error Handling and Self-Repair" in context
    assert "statement_timeout" in context
