"""Tests for model abstraction, routing, invocation auditing, and gates."""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from agent.context import build_prompt_context
from agent.llm_factory import create_llm_for_task
from agent.nodes.task_planner import task_planner
from delegation.manager import DelegationManager, default_agent_roles
from models.provider import provider_adapter_for
from models.routing import (
    ModelRouter,
    default_model_profiles,
    evaluate_model_invocation,
    fallback_decision_for_error,
    finish_invocation_record,
    pending_invocation_record,
)
from quality.manager import QualityManager
from state_management.migration import StateMigration
from state_management.validator import StateValidator


pytestmark = pytest.mark.model_routing


def _step(**extra):
    step = {
        "id": "step-1",
        "description": "Review ALTER TABLE safety",
        "status": "running",
        "dependencies": [],
        "result": None,
        "error": None,
        "phase": "propose",
        "operation_type": "schema_change",
        "risk_level": "high",
        "requires_approval": True,
        "requires_rollback_plan": True,
        "evidence_required": ["schema_summary"],
        "success_criteria": ["Risk and rollback are documented"],
        "expected_tools": ["postgres_sql_classify"],
        "tool_policy": "read_only_tools",
    }
    step.update(extra)
    return step


def _state(**extra):
    step = _step()
    state = {
        "messages": [HumanMessage(content="Review this schema change")],
        "session_id": "session-1",
        "task_stack": [step],
        "current_task_index": 0,
        "current_step_id": step["id"],
        "current_intent": {
            "id": "intent-1",
            "domain": "postgresql",
            "primary_intent": "schema_change",
            "goal": "Review a schema migration",
            "target_environment": "production",
            "target_database": "app",
            "target_objects": [{"type": "table", "name": "orders"}],
            "risk_level": "high",
        },
        "db_task_plan": {
            "id": "plan-1",
            "intent_id": "intent-1",
            "workflow": "schema_change_workflow",
            "summary": "Review schema change",
            "status": "running",
            "steps": [step],
            "assumptions": [],
            "constraints": [],
            "global_risk_level": "high",
            "requires_user_confirmation": True,
            "created_at": "now",
            "updated_at": "now",
        },
        "database_environment": {
            "environment_name": "production",
            "is_production": True,
            "target_database": "app",
        },
        "model_profiles": default_model_profiles(),
        "agent_roles": default_agent_roles(),
    }
    state.update(extra)
    return state


class FakeTool:
    name = "postgres_query_readonly"


class FakeLLM:
    def __init__(self, responses=None):
        self.bound_tools = []
        self._responses = list(responses or ["ok"])

    def bind_tools(self, tools):
        self.bound_tools = tools
        return self

    def invoke(self, _messages):
        return AIMessage(content=self._responses.pop(0) if self._responses else "ok")


def test_tool_reasoning_routes_to_tool_capable_model():
    route = ModelRouter(_state()).route("tool_reasoning", tools=[FakeTool()])
    profile = next(item for item in default_model_profiles() if item["model_id"] == route["selected_model_id"])

    assert "supports_tools" in route["required_capabilities"]
    assert route["tools_bound"] == ["postgres_query_readonly"]
    assert profile["supports_tools"] is True


def test_high_risk_planning_uses_review_model_and_forbids_downshift():
    route = ModelRouter(_state()).route("planning", structured_output_schema="TaskStep[]")
    profile = next(item for item in default_model_profiles() if item["model_id"] == route["selected_model_id"])

    assert route["policy"]["require_review_model"] is True
    assert route["policy"]["allow_downshift"] is False
    assert profile["quality_tier"] == "review"


def test_provider_aware_alias_uses_openai_profile(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("LLM_MODEL", "gpt-4o-mini")
    from agent.config import get_settings

    get_settings.cache_clear()
    try:
        route = ModelRouter(_state(model_profiles=default_model_profiles())).route("report_generation")
    finally:
        get_settings.cache_clear()

    assert route["provider"] == "openai"
    assert route["selected_model_id"] == "gpt-4o-mini"


def test_delegated_role_default_model_overrides_task_alias():
    manager = DelegationManager(_state())
    decision = manager.policy_decision(_step())
    tasks = manager.delegated_tasks_for_decision(decision, step=_step())
    task = next(item for item in tasks if item["agent_role"] == "safety_reviewer")
    state = {
        **_state(),
        "delegated_tasks": [task],
    }

    route = ModelRouter(state).route(
        "delegation_reviewer",
        delegated_task_id=task["id"],
        structured_output_schema="DelegationResult",
    )

    assert task["agent_role"] == "safety_reviewer"
    assert route["selected_model_id"] == "deepseek-reasoner"
    assert "delegated_task" in route["reason"]


def test_invocation_record_finish_and_fallback_fail_closed():
    route = ModelRouter(_state()).route("sql_safety_review", structured_output_schema="SQLSafetyReport")
    record = pending_invocation_record(route, state=_state(), structured_output_schema="SQLSafetyReport")
    finished = finish_invocation_record(record, status="failed", started_at=0.0, error=TimeoutError("slow"))
    fallback = fallback_decision_for_error(route, record, TimeoutError("slow"))

    assert finished["status"] == "failed"
    assert finished["error_type"] == "TimeoutError"
    assert fallback["decision"] == "fail_closed"
    assert fallback["allowed_by_policy"] is True


def test_provider_adapter_validates_route_and_estimates_cost():
    state = _state()
    route = ModelRouter(state).route("planning", structured_output_schema="TaskStep[]")
    profile = next(item for item in state["model_profiles"] if item["model_id"] == route["selected_model_id"])
    adapter = provider_adapter_for(route["provider"])
    record = {
        **pending_invocation_record(route),
        "input_tokens_estimate": 1000,
        "output_tokens_estimate": 500,
    }

    assert adapter.validate_model(profile, route) == []
    assert adapter.estimate_cost(record, profile) is not None


def test_quality_gates_and_report_include_model_summary():
    state = _state()
    route = ModelRouter(state).route("planning", structured_output_schema="TaskStep[]")
    profile = next(item for item in state["model_profiles"] if item["model_id"] == route["selected_model_id"])
    record = finish_invocation_record(
        pending_invocation_record(route),
        status="succeeded",
        started_at=0.0,
        output_text="ok",
        profile=profile,
    )
    evaluation = evaluate_model_invocation(record)
    manager = QualityManager({**state, "model_routes": [route], "model_invocation_records": [record], "model_evaluation_results": [evaluation]})
    routing_gate = manager.model_routing_gate(route, profile)
    output_gate = manager.model_output_quality_gate(record, evaluation)
    report = manager.quality_report(target_ref="plan-1", scope="task", gates=[routing_gate, output_gate], evaluation_results=[])

    assert routing_gate["status"] == "passed"
    assert output_gate["status"] == "passed"
    assert report["model_summary"]["model_routes"] == 1
    assert report["model_summary"]["failed_invocations"] == 0


def test_state_validator_rejects_review_policy_on_non_review_model():
    state = _state()
    route = ModelRouter(state).route("planning", structured_output_schema="TaskStep[]")
    bad_route = {
        **route,
        "selected_model_id": "deepseek-chat",
    }
    report = StateValidator({**state, "model_routes": [bad_route]}).validate()

    assert report["ok"] is False
    assert any("requires review model" in error for error in report["errors"])


def test_migration_and_context_include_model_state():
    state = _state(model_profiles=[])
    migration = StateMigration(state).migrate()
    route = ModelRouter(_state()).route("memory_compaction")

    assert migration["model_routes"] == []

    context, _ = build_prompt_context(
        {
            **_state(),
            "model_routes": [route],
            "context_token_budget": 1200,
        }
    )

    assert "Model Abstraction and Routing" in context
    assert route["selected_model_id"] in context


def test_create_llm_for_task_uses_adapter_and_persists_delegated_id(monkeypatch):
    monkeypatch.setattr("agent.llm_factory.provider_adapter_for", lambda _provider: _FakeAdapter())
    llm, route, record, profile = create_llm_for_task(
        "tool_reasoning",
        _state(),
        tools=[FakeTool()],
        delegated_task_id="delegated-1",
    )

    assert isinstance(llm, FakeLLM)
    assert llm.bound_tools
    assert record["delegated_task_id"] == "delegated-1"
    assert profile is not None
    assert route["tools_bound"] == ["postgres_query_readonly"]


def test_task_planner_repairs_malformed_structured_output(monkeypatch):
    llms = [
        FakeLLM(["not json"]),
        FakeLLM([
            '[{"id":"observe","description":"Collect schema","dependencies":[],"status":"pending",'
            '"phase":"observe","operation_type":"diagnostic","risk_level":"low",'
            '"success_criteria":["Schema collected"]}]'
        ]),
    ]

    def fake_create(task, state, tools=None, structured_output_schema=None, preferred_model=None, delegated_task_id=None):
        route = ModelRouter(state).route(task, tools=tools, structured_output_schema=structured_output_schema)
        record = pending_invocation_record(route, state=state, structured_output_schema=structured_output_schema)
        profile = next(item for item in default_model_profiles() if item["model_id"] == route["selected_model_id"])
        return llms.pop(0), route, record, profile

    monkeypatch.setattr("agent.nodes.task_planner.create_llm_for_task", fake_create)

    update = task_planner(
        _state(
            task_stack=[],
            current_step_id=None,
            database_environment={"environment_name": "staging", "is_production": False},
        )
    )

    assert update["task_stack"][0]["id"] == "observe"
    assert len(update["model_invocation_records"]) == 2
    assert all(record["status"] == "succeeded" for record in update["model_invocation_records"])


class _FakeAdapter:
    def validate_model(self, _profile, _route):
        return []

    def create_chat_model(self, _route, _profile):
        return FakeLLM()

    def estimate_cost(self, _record, _profile):
        return 0.0

    def normalize_error(self, error):
        return {"error_type": type(error).__name__, "message": str(error)}
