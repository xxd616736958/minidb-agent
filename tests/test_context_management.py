"""Tests for PostgreSQL context management utilities."""

from agent.context import (
    build_context_snapshot,
    build_db_working_set,
    build_prompt_context,
    build_result_digest,
    build_step_context_packet,
    compact_prompt_context,
)
from agent.nodes.llm_node import build_system_prompt


def _step():
    return {
        "id": "observe",
        "description": "Inspect orders table",
        "status": "running",
        "dependencies": [],
        "result": None,
        "error": None,
        "phase": "observe",
        "operation_type": "diagnostic",
        "risk_level": "low",
        "requires_approval": False,
        "requires_rollback_plan": False,
        "evidence_required": ["explain_plan"],
        "success_criteria": ["EXPLAIN is available"],
        "expected_tools": ["postgres_read"],
        "tool_policy": "read_only_tools",
    }


def _state(**extra):
    state = {
        "current_step_id": "observe",
        "current_task_index": 0,
        "task_stack": [_step()],
        "current_intent": {
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
            "output_contract": {},
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
        },
        "db_task_plan": {
            "id": "plan-1",
            "intent_id": "intent-1",
            "workflow": "performance_diagnosis_workflow",
            "summary": "test",
            "status": "running",
            "steps": [_step()],
            "assumptions": [],
            "constraints": ["只读"],
            "global_risk_level": "low",
            "requires_user_confirmation": False,
            "created_at": "now",
            "updated_at": "now",
        },
        "db_observations": [],
        "approval_decisions": [],
        "verification_results": [],
    }
    state.update(extra)
    return state


def test_step_context_packet_prioritizes_current_step_and_constraints():
    packet = build_step_context_packet(_state())

    assert packet["step_id"] == "observe"
    assert packet["tool_policy"] == "read_only_tools"
    assert "只读" in packet["user_constraints"]
    assert "DDL" in packet["blocked_actions"]
    assert "explain_plan" in packet["missing_context"]


def test_step_context_packet_maps_natural_language_evidence_labels():
    step = {
        **_step(),
        "evidence_required": ["pg_stat_statements 查询结果", "表结构", "索引列表", "行数估算"],
    }
    observation_top = {
        "id": "obs-top",
        "step_id": "observe",
        "type": "top_queries",
        "source_tool": "postgres_top_queries",
        "summary": "Collected 10 top query row(s).",
        "payload": {"success": True, "queries": [{"query_preview": "SELECT 1"}]},
        "created_at": "now",
    }
    observation_object = {
        "id": "obs-object",
        "step_id": "observe",
        "type": "object_detail",
        "source_tool": "postgres_object_detail",
        "summary": "Collected details for public.orders.",
        "payload": {"success": True, "indexes": [{"name": "idx_orders"}]},
        "created_at": "now",
    }
    observation_stats = {
        "id": "obs-stats",
        "step_id": "observe",
        "type": "query_result",
        "source_tool": "postgres_query_readonly",
        "summary": "Read-only query returned 1 row(s).",
        "payload": {"success": True, "rows": [{"n_live_tup": 1000}]},
        "created_at": "now",
    }

    packet = build_step_context_packet(
        _state(
            task_stack=[step],
            db_task_plan={**_state()["db_task_plan"], "steps": [step]},
            db_observations=[observation_top, observation_object, observation_stats],
        )
    )

    assert packet["missing_context"] == []


def test_db_working_set_from_intent_targets():
    working_set = build_db_working_set(_state())

    assert working_set["target_environment"] == "staging"
    assert working_set["target_database"] == "app"
    assert "orders" in working_set["tables"]
    assert working_set["known_queries"]


def test_result_digest_masks_sensitive_fields():
    observation = {
        "id": "obs-1",
        "step_id": "observe",
        "type": "query_result",
        "source_tool": "postgres_read",
        "summary": "rows",
        "payload": {
            "rows": [
                {"id": 1, "email": "alice@example.com", "amount": 10},
                {"id": 2, "email": "bob@example.com", "amount": 20},
                {"id": 3, "email": "c@example.com", "amount": 30},
                {"id": 4, "email": "d@example.com", "amount": 40},
                {"id": 5, "email": "e@example.com", "amount": 50},
                {"id": 6, "email": "f@example.com", "amount": 60},
            ]
        },
        "created_at": "now",
    }

    digest = build_result_digest(observation)

    assert digest["row_count"] == 6
    assert digest["truncation_applied"] is True
    assert "email" in digest["sensitive_fields_masked"]
    assert "***" in digest["sample_rows"][0]["email"]


def test_prompt_context_contains_step_packet_and_respects_budget():
    context, packet = build_prompt_context(_state(context_token_budget=120))

    assert packet["step_id"] == "observe"
    assert "Current Step Context Packet" in context
    assert "Context truncated" in context or len(context) <= 120 * 4 + 80


def test_context_snapshot_references_structured_state():
    observation = {
        "id": "obs-1",
        "step_id": "observe",
        "type": "explain_plan",
        "source_tool": "postgres_read",
        "summary": "explain",
        "payload": {},
        "created_at": "now",
    }
    state = _state(db_observations=[observation])

    snapshot = build_context_snapshot(state)

    assert snapshot["intent_id"] == "intent-1"
    assert snapshot["plan_id"] == "plan-1"
    assert snapshot["current_step_id"] == "observe"
    assert "obs-1" in snapshot["observation_ids"]


def test_compact_prompt_context_truncates_low_priority_tail():
    compacted = compact_prompt_context("x" * 1000, budget=20)

    assert "Context truncated" in compacted


def test_system_prompt_uses_minidb_identity_and_workspace():
    prompt = build_system_prompt(
        _state(
            workspace_profile={
                "root_path": "/tmp/minidb-workspace",
                "default_cwd": "/tmp/minidb-workspace",
            }
        )
    )

    assert "MiniDB Agent" in prompt
    assert "PostgreSQL management agent" in prompt
    assert "terminal-operating programming assistant" not in prompt
    assert "Workspace directory: /tmp/minidb-workspace" in prompt
