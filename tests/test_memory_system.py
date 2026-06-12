"""Tests for PostgreSQL-safe long-term memory system."""

from datetime import datetime, timedelta, timezone

from langchain_core.messages import AIMessage

from agent.context import build_prompt_context
from agent.nodes.agent_loop import tool_policy_gate
from agent.nodes.intent import intent_analyzer, intent_validator
from memory.consolidator import consolidate_memories, generate_memory_candidates
from memory.schema import (
    build_memory_query,
    candidate_from_user_preference,
    make_memory_record,
    memory_read_gate,
    memory_write_gate,
)
from memory.store import MemoryStore, get_memory_store


def setup_function():
    store = get_memory_store()
    store.records.clear()
    store.save()


def _state(**extra):
    state = {
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
            "input_artifacts": [],
            "output_contract": {},
            "missing_slots": [],
            "assumptions": [],
            "constraints": ["只读"],
            "risk_level": "low",
            "requires_clarification": False,
            "requires_approval": False,
            "requires_rollback_plan": False,
            "evidence_needed": [],
            "suggested_workflow": "performance_diagnosis_workflow",
            "next_action": "read_only_observe",
        },
        "step_context": {
            "step_id": "observe",
            "phase": "observe",
            "description": "observe",
            "risk_level": "low",
            "tool_policy": "read_only_tools",
            "success_criteria": [],
            "user_constraints": ["只读"],
            "relevant_observations": [],
            "relevant_approvals": [],
            "relevant_verifications": [],
            "allowed_actions": [],
            "blocked_actions": [],
            "missing_context": [],
        },
        "db_observations": [],
        "verification_results": [],
        "approval_decisions": [],
    }
    state.update(extra)
    return state


def _step(**extra):
    step = {
        "id": "execute",
        "description": "Execute approved SQL",
        "status": "running",
        "dependencies": [],
        "result": None,
        "error": None,
        "phase": "execute",
        "operation_type": "data_change",
        "risk_level": "high",
        "requires_approval": True,
        "requires_rollback_plan": True,
        "evidence_required": [],
        "success_criteria": ["SQL executed"],
        "expected_tools": ["postgres_write"],
        "tool_policy": "write_tools_after_approval",
    }
    step.update(extra)
    return step


def test_memory_write_gate_blocks_secret_content():
    record = make_memory_record(
        kind="fact",
        scope="user",
        namespace="test",
        summary="password is abc",
        source="user_confirmed",
        sensitivity="secret",
    )
    candidate = {
        "id": "cand-1",
        "proposed_record": record,
        "reason": "test",
        "requires_user_confirmation": False,
        "write_decision": "auto_write",
    }

    allowed, reason = memory_write_gate(candidate)

    assert allowed is False
    assert "secret" in reason


def test_memory_write_gate_requires_confirmation_for_unapproved_assumption():
    record = make_memory_record(
        kind="assumption",
        scope="project",
        namespace="test",
        summary="Production requires maintenance window",
        source="agent_inferred",
        confidence=0.8,
    )
    candidate = {
        "id": "cand-2",
        "proposed_record": record,
        "reason": "test",
        "requires_user_confirmation": True,
        "write_decision": "pending",
    }

    allowed, reason = memory_write_gate(candidate)

    assert allowed is False
    assert "approval" in reason or "confirmation" in reason


def test_memory_write_gate_blocks_sensitive_pii_by_default():
    record = make_memory_record(
        kind="fact",
        scope="database",
        namespace="test",
        summary="customer email is alice@example.com",
        source="tool_observed",
    )
    candidate = {
        "id": "cand-pii",
        "proposed_record": record,
        "reason": "test",
        "requires_user_confirmation": False,
        "write_decision": "auto_write",
    }

    allowed, reason = memory_write_gate(candidate)

    assert allowed is False
    assert "sensitive" in reason


def test_memory_read_gate_rejects_expired_schema_memory():
    observed = datetime.now(timezone.utc) - timedelta(days=2)
    record = make_memory_record(
        kind="schema_summary",
        scope="database",
        namespace="staging:app",
        summary="orders schema",
        source="tool_observed",
        ttl_seconds=1,
    )
    record["observed_at"] = observed.isoformat()
    record["expires_at"] = (observed + timedelta(seconds=1)).isoformat()

    allowed, reason = memory_read_gate(record, build_memory_query(_state()))

    assert allowed is False
    assert reason == "record expired"


def test_memory_read_gate_rejects_sensitive_and_cross_environment_memory():
    sensitive = make_memory_record(
        kind="fact",
        scope="database",
        namespace="prod:app",
        summary="Contains customer email samples",
        payload={"target_environment": "staging"},
        source="tool_observed",
        sensitivity="sensitive",
    )

    allowed, reason = memory_read_gate(sensitive, build_memory_query(_state()))

    assert allowed is False
    assert reason == "sensitivity too high"

    internal = make_memory_record(
        kind="fact",
        scope="database",
        namespace="prod:app",
        summary="Production schema note",
        payload={"target_environment": "production"},
        source="tool_observed",
        sensitivity="internal",
    )
    allowed, reason = memory_read_gate(internal, build_memory_query(_state()))

    assert allowed is False
    assert reason == "target environment mismatch"


def test_memory_store_search_filters_by_scope_and_environment():
    store = MemoryStore()
    record = make_memory_record(
        kind="experience",
        scope="database",
        namespace="staging:app",
        summary="orders slow query needs explain",
        payload={"target_environment": "staging"},
        source="tool_observed",
        confidence=0.8,
    )
    store.upsert(record)

    results = store.search(build_memory_query(_state()), limit=5)

    assert results[0]["id"] == record["id"]


def test_memory_store_prioritizes_safety_memory_without_object_match():
    store = MemoryStore()
    safety = make_memory_record(
        kind="prohibition",
        scope="user",
        namespace="safety",
        summary="Production database is read-only unless explicitly approved today",
        source="user_confirmed",
        confidence=0.95,
    )
    schema = make_memory_record(
        kind="schema_summary",
        scope="database",
        namespace="staging:app",
        summary="orders table has created_at index",
        payload={"target_environment": "staging", "target_database": "app"},
        source="tool_observed",
        confidence=0.8,
    )
    store.upsert(schema)
    store.upsert(safety)

    results = store.search(build_memory_query(_state()), limit=5)

    assert results[0]["id"] == safety["id"]


def test_memory_store_upsert_deprecates_superseded_schema_summary():
    store = MemoryStore()
    first = make_memory_record(
        kind="schema_summary",
        scope="database",
        namespace="staging:app",
        summary="orders(id)",
        payload={
            "memory_key": "schema:orders",
            "target_environment": "staging",
            "target_database": "app",
        },
        source="tool_observed",
    )
    second = make_memory_record(
        kind="schema_summary",
        scope="database",
        namespace="staging:app",
        summary="orders(id, created_at)",
        payload={
            "memory_key": "schema:orders",
            "target_environment": "staging",
            "target_database": "app",
        },
        source="tool_observed",
    )

    store.upsert(first)
    store.upsert(second)

    assert store.get(first["id"])["status"] == "deprecated"
    assert first["id"] in store.get(second["id"])["supersedes"]


def test_memory_store_persists_records_to_json(tmp_path):
    path = tmp_path / "memory_records.json"
    store = MemoryStore(path=str(path))
    record = make_memory_record(
        kind="preference",
        scope="user",
        namespace="user",
        summary="Prefer concise reports",
        source="user_confirmed",
        sensitivity="internal",
    )

    store.upsert(record)
    reloaded = MemoryStore(path=str(path))

    assert reloaded.get(record["id"])["summary"] == "Prefer concise reports"


def test_generate_memory_candidates_from_verified_observation():
    observation = {
        "id": "obs-1",
        "step_id": "observe",
        "type": "explain_plan",
        "source_tool": "postgres_read",
        "summary": "Seq Scan on orders",
        "payload": {},
        "created_at": "now",
    }
    verification = {
        "id": "verify-1",
        "step_id": "observe",
        "status": "passed",
        "criteria_checked": ["EXPLAIN is available"],
        "evidence_ids": ["obs-1"],
        "summary": "passed",
        "created_at": "now",
    }

    candidates = generate_memory_candidates(
        _state(db_observations=[observation], verification_results=[verification])
    )

    assert any(c["proposed_record"]["kind"] == "experience" for c in candidates)
    assert any(c["proposed_record"]["kind"] == "prohibition" for c in candidates)


def test_consolidate_memories_writes_allowed_candidates():
    store = get_memory_store()
    preference = candidate_from_user_preference("Prefer Markdown database reports")

    allowed, _ = memory_write_gate(preference)
    assert allowed is True
    store.upsert(preference["proposed_record"])

    context, _ = build_prompt_context(_state())

    assert "Prefer Markdown" in context


def test_consolidate_memories_from_state_writes_to_store():
    store = get_memory_store()
    observation = {
        "id": "obs-2",
        "step_id": "observe",
        "type": "schema_summary",
        "source_tool": "postgres_read",
        "summary": "orders(id, created_at)",
        "payload": {},
        "created_at": "now",
    }
    verification = {
        "id": "verify-2",
        "step_id": "observe",
        "status": "passed",
        "criteria_checked": ["schema available"],
        "evidence_ids": ["obs-2"],
        "summary": "passed",
        "created_at": "now",
    }

    updates = consolidate_memories(
        _state(db_observations=[observation], verification_results=[verification])
    )

    assert updates["memory_records_written"]
    assert store.records


def test_intent_validator_applies_safety_memory_to_constraints():
    store = get_memory_store()
    safety = make_memory_record(
        kind="prohibition",
        scope="user",
        namespace="safety",
        summary="Production database must stay read-only",
        source="user_confirmed",
        confidence=0.95,
    )
    store.upsert(safety)
    state = _state(
        messages=[],
        current_intent={
            **_state()["current_intent"],
            "constraints": [],
            "missing_slots": [],
            "requires_clarification": False,
        },
    )

    result = intent_validator(state)

    assert any("Production database must stay read-only" in item for item in result["current_intent"]["constraints"])


def test_intent_analyzer_persists_explicit_memory_request_with_write_gate(monkeypatch):
    class FakeLLM:
        def invoke(self, messages):
            return type(
                "Resp",
                (),
                {
                    "content": """{
                      "domain": "postgresql",
                      "primary_intent": "documentation",
                      "candidate_intents": ["documentation"],
                      "confidence": 0.8,
                      "goal": "Remember report preference",
                      "user_language_summary": "Remember report preference",
                      "operation_nature": "documentation",
                      "target_environment": "unknown",
                      "target_database": null,
                      "target_objects": [],
                      "input_artifacts": [],
                      "output_contract": {},
                      "missing_slots": [],
                      "assumptions": [],
                      "constraints": [],
                      "risk_level": "low",
                      "requires_clarification": false,
                      "requires_approval": false,
                      "requires_rollback_plan": false,
                      "evidence_needed": [],
                      "suggested_workflow": "documentation_workflow",
                      "next_action": "plan"
                    }"""
                },
            )()

    monkeypatch.setattr("agent.nodes.intent.create_llm_no_tools", lambda **_: FakeLLM())
    state = _state(messages=[type("Human", (), {"type": "human", "content": "记住以后数据库报告使用 Markdown"})()])

    result = intent_analyzer(state)

    assert result["memory_records_written"]
    assert result["memory_records_written"][0]["kind"] == "preference"
    assert "Markdown" in result["memory_records_written"][0]["summary"]


def test_explicit_memory_request_does_not_store_credentials(monkeypatch):
    class FakeLLM:
        def invoke(self, messages):
            return type("Resp", (), {"content": "{}"})()

    monkeypatch.setattr("agent.nodes.intent.create_llm_no_tools", lambda **_: FakeLLM())
    state = _state(messages=[type("Human", (), {"type": "human", "content": "记住数据库 password is abc123"})()])

    result = intent_analyzer(state)

    assert "memory_records_written" not in result
    assert get_memory_store().records == {}


def test_tool_policy_gate_blocks_write_sql_from_safety_memory_even_after_old_approval():
    safety = make_memory_record(
        kind="prohibition",
        scope="user",
        namespace="safety",
        summary="User requires read-only PostgreSQL work",
        source="user_confirmed",
        confidence=0.95,
    )
    msg = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "postgres_execute",
                "args": {"sql": "UPDATE orders SET status = 'done' WHERE id = 1"},
                "id": "call-1",
            }
        ],
    )
    state = {
        **_state(),
        "messages": [msg],
        "task_stack": [_step()],
        "current_step_id": "execute",
        "current_task_index": 0,
        "retrieved_memories": [safety],
        "approval_decisions": [
            {
                "id": "approval-old",
                "step_id": "different-step",
                "status": "approved",
                "risk_level": "high",
                "target_environment": "staging",
                "sql_preview": "UPDATE orders SET status = 'done'",
                "impact_summary": None,
                "rollback_summary": None,
                "user_message": None,
                "created_at": "now",
                "resolved_at": "now",
            }
        ],
    }

    result = tool_policy_gate(state)

    assert result["policy_violation"]["step_id"] == "execute"
    assert "SafetyMemory" in result["policy_violation"]["message"]
