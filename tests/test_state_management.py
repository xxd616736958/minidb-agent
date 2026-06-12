"""Tests for versioned state management and recovery."""

from state_management.manager import STATE_SCHEMA_VERSION, StateManager
from state_management.migration import StateMigration
from state_management.recovery import StateRecovery
from state_management.validator import StateValidator


def _step(step_id="observe", status="running", **extra):
    step = {
        "id": step_id,
        "description": "Observe database",
        "status": status,
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
        "session_id": "session-1",
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
    }
    state.update(extra)
    return state


def test_state_migration_adds_metadata_and_runtime():
    update = StateMigration(_state()).migrate()

    assert update["state_schema_version"] == STATE_SCHEMA_VERSION
    assert update["state_metadata"]["schema_version"] == STATE_SCHEMA_VERSION
    assert update["state_metadata"]["recovery_mode"] == "migrated"
    assert update["db_task_runtime"]["current_step_id"] == "observe"


def test_state_manager_syncs_plan_and_stack():
    state = _state()
    steps = [_step("observe", status="completed"), _step("diagnose", status="running")]

    update = StateManager(state).sync_plan_and_stack(steps, current_idx=1, plan_status="running")

    assert update["task_stack"][1]["id"] == "diagnose"
    assert update["db_task_plan"]["steps"][1]["id"] == "diagnose"
    assert update["current_task_index"] == 1
    assert update["current_step_id"] == "diagnose"
    assert update["db_task_runtime"]["current_step_id"] == "diagnose"


def test_state_validator_detects_plan_stack_mismatch():
    state = _state(task_stack=[_step("different")])

    report = StateValidator(state).validate()

    assert report["ok"] is False
    assert any("not aligned" in error for error in report["errors"])


def test_state_validator_blocks_production_write_policy():
    state = _state(
        database_environment={"environment_name": "production", "is_production": True},
        runtime_policy={"allow_database_writes": True},
    )

    report = StateValidator(state).validate()

    assert report["ok"] is False
    assert any("Production" in error for error in report["errors"])


def test_replay_policy_marks_write_tools_non_replayable():
    manager = StateManager(_state())

    readonly = manager.replay_policy_for_tool("postgres_query_readonly", "call-read")
    write = manager.replay_policy_for_tool("postgres_execute_write", "call-write")

    assert readonly["replayable"] is True
    assert write["replayable"] is False
    assert write["requires_new_approval"] is True


def test_state_recovery_builds_environment_runtime_integrity_and_summary(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state = _state(
        tool_invocation_records=[
            {
                "id": "tool-1",
                "call_id": "call-write",
                "tool_name": "postgres_execute_write",
                "step_id": "observe",
                "intent_id": "intent-1",
                "args_digest": {},
                "policy_decision": {
                    "call_id": "call-write",
                    "tool_name": "postgres_execute_write",
                    "decision": "allow",
                    "reason": "approved",
                    "risk_level": "high",
                    "approval_required": True,
                    "approval_payload": None,
                },
                "approval_id": "approval-1",
                "started_at": "now",
                "ended_at": None,
                "status": "pending",
                "duration_ms": None,
                "result_ref": None,
                "observation_ids": [],
                "error_type": None,
                "error_message": None,
            }
        ]
    )

    update = StateRecovery(state).recover()

    assert update["state_metadata"]["recovery_mode"] == "resumed"
    assert update["db_task_runtime"]["target_database"] is not None or update["db_task_runtime"]["target_environment"]
    assert update["state_integrity_reports"][0]["ok"] in {True, False}
    assert "Recovered task" in update["recovery_summary"]
    assert update["replay_policies"][0]["replayable"] is False
