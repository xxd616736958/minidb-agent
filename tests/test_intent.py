"""Tests for PostgreSQL task understanding and intent validation."""

from agent.nodes.intent import (
    clarification_gate,
    intent_validator,
    normalize_intent,
)


def test_normalize_documentation_intent():
    intent = normalize_intent(
        {
            "domain": "documentation",
            "primary_intent": "documentation",
            "candidate_intents": ["documentation"],
            "confidence": 0.8,
            "goal": "Write a database migration test report",
            "user_language_summary": "Write a migration test report",
            "operation_nature": "documentation",
            "target_environment": "unknown",
            "target_database": None,
            "target_objects": [],
            "input_artifacts": [],
            "output_contract": {"type": "test_report", "format": "markdown"},
            "missing_slots": ["test_result_source"],
            "assumptions": [],
            "constraints": [],
            "risk_level": "low",
            "requires_clarification": True,
            "requires_approval": False,
            "requires_rollback_plan": False,
            "evidence_needed": [],
            "suggested_workflow": "documentation_workflow",
            "next_action": "ask_clarification",
        },
        "帮我写一份数据库迁移测试报告",
    )

    assert intent["primary_intent"] == "documentation"
    assert intent["output_contract"]["type"] == "test_report"
    assert intent["risk_level"] == "low"


def test_validator_upgrades_delete_without_where_to_critical():
    intent = normalize_intent(
        {
            "domain": "postgresql",
            "primary_intent": "data_change",
            "candidate_intents": ["data_change"],
            "confidence": 0.9,
            "goal": "Delete old rows from orders",
            "user_language_summary": "Delete old rows from orders",
            "operation_nature": "write_data",
            "target_environment": "production",
            "target_database": "app",
            "target_objects": [{"type": "table", "name": "orders"}],
            "risk_level": "medium",
            "suggested_workflow": "data_change_workflow",
            "next_action": "plan",
        },
        "delete old rows from orders",
    )
    state = {
        "messages": [],
        "current_intent": intent,
    }

    result = intent_validator(state)
    validated = result["current_intent"]

    assert validated["risk_level"] == "critical"
    assert validated["requires_approval"] is True
    assert validated["requires_rollback_plan"] is True
    assert "where_condition_or_safety_filter" in validated["missing_slots"]
    assert validated["next_action"] == "ask_clarification"


def test_validator_requires_clarification_for_ambiguous_postgres_task():
    intent = normalize_intent({}, "数据库最近很慢，帮我看看")
    state = {
        "messages": [],
        "current_intent": intent,
    }

    result = intent_validator(state)
    validated = result["current_intent"]

    assert validated["domain"] == "postgresql"
    assert validated["requires_clarification"] is True
    assert "target_environment" in validated["missing_slots"]


def test_validator_detects_chinese_data_cleanup_as_high_risk():
    intent = normalize_intent({}, "帮我清理掉 orders 表里的老数据")
    state = {
        "messages": [],
        "current_intent": intent,
    }

    result = intent_validator(state)
    validated = result["current_intent"]

    assert validated["domain"] == "postgresql"
    assert validated["operation_nature"] == "write_data"
    assert validated["requires_approval"] is True
    assert validated["requires_rollback_plan"] is True


def test_clarification_gate_returns_message_and_pending_request():
    intent = normalize_intent({}, "数据库最近很慢，帮我看看")
    intent["missing_slots"] = ["target_environment", "sql_or_symptom"]
    intent["requires_clarification"] = True
    intent["risk_level"] = "low"

    result = clarification_gate({"current_intent": intent})

    assert result["pending_clarification"]["status"] == "pending"
    assert len(result["pending_clarification"]["questions"]) == 2
    assert result["messages"]
