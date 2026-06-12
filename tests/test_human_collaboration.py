"""Tests for human collaboration protocol state."""

from langchain_core.messages import AIMessage

from agent.context import build_prompt_context
from agent.nodes.agent_loop import tool_policy_gate
from agent.nodes.intent import clarification_gate, intent_validator, normalize_intent
from agent.nodes.task_planner import build_db_task_plan, validate_and_normalize_plan
from collaboration.manager import CollaborationManager
from execution.environment import build_database_environment_profile, build_runtime_policy
from state_management.migration import StateMigration
from state_management.validator import StateValidator
from tools.builtin.postgres import PostgresExecuteWriteTool
from tools.registry import registry


def setup_function():
    registry.clear()


def _intent(**extra):
    intent = {
        "id": "intent-1",
        "domain": "postgresql",
        "primary_intent": "performance_diagnosis",
        "candidate_intents": ["performance_diagnosis"],
        "confidence": 0.9,
        "goal": "Diagnose slow orders query",
        "user_language_summary": "Diagnose slow orders query",
        "operation_nature": "diagnostic",
        "target_environment": "staging",
        "target_database": "app",
        "target_objects": [{"type": "table", "name": "orders"}],
        "input_artifacts": [{"type": "sql", "value": "select * from orders"}],
        "output_contract": {"format": "markdown"},
        "missing_slots": [],
        "assumptions": [],
        "constraints": ["只读"],
        "risk_level": "low",
        "requires_clarification": False,
        "requires_approval": False,
        "requires_rollback_plan": False,
        "evidence_needed": ["execution_plan"],
        "suggested_workflow": "performance_diagnosis_workflow",
        "next_action": "read_only_observe",
    }
    intent.update(extra)
    return intent


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
        "current_intent": _intent(),
        "selected_workflow": "performance_diagnosis_workflow",
        "task_stack": steps,
        "current_task_index": 0,
        "current_step_id": "observe",
        "db_task_plan": {
            "id": "plan-1",
            "intent_id": "intent-1",
            "workflow": "performance_diagnosis_workflow",
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
    }
    state.update(extra)
    return state


def test_intent_validator_emits_task_card_and_event():
    result = intent_validator({"current_intent": _intent(), "messages": []})

    assert result["task_card"]["intent_id"] == "intent-1"
    assert result["task_card"]["risk_level"] == "low"
    assert result["collaboration_events"][0]["event_type"] == "task_card_shown"


def test_clarification_gate_records_collaboration_event():
    intent = _intent(
        missing_slots=["target_environment"],
        requires_clarification=True,
        target_environment="unknown",
    )

    result = clarification_gate({"current_intent": intent})

    assert result["pending_clarification"]["status"] == "pending"
    assert result["collaboration_events"][0]["event_type"] == "clarification_requested"


def test_plan_review_marks_high_risk_plan_pending():
    intent = _intent(
        primary_intent="schema_change",
        operation_nature="schema_change",
        risk_level="high",
        requires_approval=True,
        requires_rollback_plan=True,
        suggested_workflow="schema_change_workflow",
    )
    state = {"current_intent": intent, "selected_workflow": "schema_change_workflow"}
    tasks = validate_and_normalize_plan([], state)
    plan = build_db_task_plan(tasks, state)

    update = CollaborationManager(state).plan_review_update(plan)

    assert update["plan_review"]["status"] == "pending"
    assert update["collaboration_events"][0]["event_type"] == "plan_shown"


def test_tool_policy_gate_emits_safety_block_event_for_denied_write():
    msg = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "postgres_execute_write",
                "args": {"sql": "DELETE FROM orders"},
                "id": "call-deny",
            }
        ],
    )

    update = tool_policy_gate(_state(messages=[msg]))

    event_types = [event["event_type"] for event in update["collaboration_events"]]
    assert "tool_call_shown" in event_types
    assert "safety_block_explained" in event_types


def test_approval_card_is_bound_to_pending_approval():
    registry.register(PostgresExecuteWriteTool())
    db_env = build_database_environment_profile("postgresql://user:secret@dev.example.com/app")
    db_env["environment_name"] = "dev"
    db_env["is_production"] = False
    db_env["allow_write_tools"] = True
    step = _step(
        "execute",
        phase="execute",
        tool_policy="write_tools_after_approval",
        operation_type="data_change",
        risk_level="high",
        requires_approval=True,
        success_criteria=["one row updated"],
    )
    msg = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "postgres_execute_write",
                "args": {
                    "sql": "UPDATE orders SET status = 'done' WHERE id = 1",
                    "target_environment": "dev",
                    "impact_summary": "Update one row",
                    "rollback_summary": "Restore old status",
                },
                "id": "call-approval",
            }
        ],
    )
    state = _state(
        messages=[msg],
        task_stack=[step],
        current_step_id="execute",
        current_task_index=0,
        db_task_plan={**_state()["db_task_plan"], "steps": [step]},
        step_context={
            "step_id": "execute",
            "phase": "execute",
            "description": "Execute database change",
            "risk_level": "high",
            "tool_policy": "write_tools_after_approval",
            "success_criteria": ["one row updated"],
            "user_constraints": [],
            "relevant_observations": [],
            "relevant_approvals": [],
            "relevant_verifications": [],
            "allowed_actions": ["Execute approved database write tools"],
            "blocked_actions": [],
            "missing_context": [],
        },
        database_environment=db_env,
        runtime_policy=build_runtime_policy(db_env),
    )

    update = tool_policy_gate(state)

    assert update["loop_status"] == "waiting_for_approval"
    assert update["approval_card"]["approval_id"] == update["pending_approval"]["id"]
    assert update["approval_card"]["replay_policy"] == "requires_new_approval_for_changed_sql"
    assert "approve" in update["approval_card"]["options"]


def test_prompt_context_contains_human_collaboration_section():
    task_update = CollaborationManager(_state()).task_card_update(_intent())
    context, _ = build_prompt_context(_state(**task_update, context_token_budget=400))

    assert "Human Collaboration" in context
    assert "Task card" in context or "task_card" in context


def test_state_migration_and_validator_cover_collaboration_fields():
    state = _state(
        pending_approval={
            "id": "approval-1",
            "step_id": "observe",
            "status": "pending",
            "risk_level": "high",
            "target_environment": "staging",
            "sql_preview": "UPDATE orders SET status='done' WHERE id=1",
            "sql_hash": "hash-1",
            "impact_summary": "Update one row",
            "rollback_summary": "Restore old status",
            "verification_criteria": ["done"],
            "user_message": "Tool=postgres_execute_write",
            "created_at": "now",
            "resolved_at": None,
        }
    )

    migration = StateMigration(state).migrate()
    report = StateValidator({**state, **migration}).validate()

    assert migration["collaboration_events"] == []
    assert migration["user_feedback"] == []
    assert report["ok"] is True
    assert any("approval_card" in warning for warning in report["warnings"])
