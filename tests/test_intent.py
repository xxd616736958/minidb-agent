"""Tests for PostgreSQL task understanding and intent validation."""

import json

from langchain_core.messages import AIMessage, HumanMessage

from agent.nodes.intent import (
    clarification_gate,
    intent_analyzer,
    intent_validator,
    normalize_intent,
)


def _db_env():
    return {
        "environment_name": "dev",
        "target_database": "db_agent",
        "safe_host_label": "127.0.0.1",
        "safe_user_label": "db_agent",
        "access_mode": "write_after_approval",
        "is_production": False,
        "default_statement_timeout_ms": 30000,
        "default_lock_timeout_ms": 5000,
        "max_result_rows": 100,
        "allow_write_tools": True,
        "require_backup_check_for_writes": False,
        "credential_ref": "env:POSTGRES_TARGET_URL",
    }


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


def test_intent_analyzer_sends_conversation_and_config_packet(monkeypatch):
    captured = {}

    class FakeLLM:
        def invoke(self, messages):
            captured["messages"] = messages
            return AIMessage(
                content=json.dumps(
                    {
                        "domain": "postgresql",
                        "primary_intent": "performance_diagnosis",
                        "candidate_intents": ["performance_diagnosis", "read_only_analysis"],
                        "confidence": 0.9,
                        "goal": "Diagnose PostgreSQL performance using safe observation.",
                        "user_language_summary": "User delegated safe diagnostic scope after a prior clarification.",
                        "operation_nature": "diagnostic",
                        "target_environment": "unknown",
                        "target_database": None,
                        "target_objects": [],
                        "input_artifacts": [],
                        "output_contract": {},
                        "missing_slots": ["target_environment", "target_database", "time_range"],
                        "assumptions": [],
                        "constraints": [],
                        "risk_level": "low",
                        "requires_clarification": True,
                        "requires_approval": False,
                        "requires_rollback_plan": False,
                        "evidence_needed": ["top_queries"],
                        "suggested_workflow": "performance_diagnosis_workflow",
                        "next_action": "ask_clarification",
                    }
                )
            )

    monkeypatch.setattr("agent.nodes.intent.create_llm_no_tools", lambda **_: FakeLLM())
    state = {
        "messages": [
            HumanMessage(content="Diagnose the current PostgreSQL performance issue."),
            HumanMessage(content="Use your judgement and continue safely."),
        ],
        "current_intent": {
            "id": "intent-existing",
            "primary_intent": "performance_diagnosis",
            "missing_slots": ["time_range"],
        },
        "pending_clarification": {
            "id": "clarify-1",
            "questions": ["What time range should be inspected?"],
            "missing_slots": ["time_range"],
            "reason": "missing",
            "status": "pending",
        },
        "database_environment": _db_env(),
        "runtime_policy": {"require_approval_for_database_write": True},
    }

    result = intent_analyzer(state)
    packet = json.loads(captured["messages"][1].content)
    intent = result["current_intent"]

    assert packet["configured_database_environment"]["target_database"] == "db_agent"
    assert packet["pending_clarification"]["id"] == "clarify-1"
    assert packet["current_intent"]["id"] == "intent-existing"
    assert packet["conversation"][-1]["role"] == "user"
    assert intent["target_environment"] == "dev"
    assert intent["target_database"] == "db_agent"
    assert intent["requires_clarification"] is False
    assert intent["missing_slots"] == []
    assert intent["next_action"] == "read_only_observe"


def test_validator_uses_configured_context_for_model_readonly_intent():
    intent = normalize_intent(
        {
            "domain": "postgresql",
            "primary_intent": "read_only_analysis",
            "candidate_intents": ["read_only_analysis"],
            "confidence": 0.9,
            "goal": "Inspect PostgreSQL state with read-only tools.",
            "user_language_summary": "Inspect PostgreSQL state with read-only tools.",
            "operation_nature": "read_only",
            "target_environment": "unknown",
            "target_database": None,
            "target_objects": [],
            "input_artifacts": [],
            "missing_slots": ["target_environment", "target_database", "target_objects_or_sql"],
            "risk_level": "low",
            "requires_clarification": True,
            "requires_approval": False,
            "requires_rollback_plan": False,
            "evidence_needed": ["connection_info"],
            "suggested_workflow": "read_only_analysis_workflow",
            "next_action": "ask_clarification",
        },
        "Inspect PostgreSQL state with read-only tools.",
    )
    state = {
        "messages": [HumanMessage(content="Inspect PostgreSQL state with read-only tools.")],
        "current_intent": intent,
        "database_environment": _db_env(),
    }

    result = intent_validator(state)
    validated = result["current_intent"]

    assert validated["target_environment"] == "dev"
    assert validated["target_database"] == "db_agent"
    assert validated["primary_intent"] == "read_only_analysis"
    assert validated["requires_clarification"] is False
    assert validated["missing_slots"] == []
    assert validated["next_action"] == "read_only_observe"


def test_validator_allows_configured_low_risk_diagnostic_without_optional_scope():
    intent = normalize_intent(
        {
            "domain": "postgresql",
            "primary_intent": "performance_diagnosis",
            "candidate_intents": ["performance_diagnosis"],
            "confidence": 0.85,
            "goal": "Diagnose PostgreSQL performance from available read-only evidence.",
            "user_language_summary": "Diagnose PostgreSQL performance from available read-only evidence.",
            "operation_nature": "diagnostic",
            "target_environment": "unknown",
            "target_database": None,
            "target_objects": [],
            "input_artifacts": [],
            "missing_slots": [
                "target_environment",
                "target_database",
                "sql_or_symptom",
                "time_range",
                "threshold",
            ],
            "risk_level": "low",
            "requires_clarification": True,
            "requires_approval": False,
            "requires_rollback_plan": False,
            "evidence_needed": [],
            "suggested_workflow": "performance_diagnosis_workflow",
            "next_action": "ask_clarification",
        },
        "Diagnose PostgreSQL performance from available read-only evidence.",
    )
    state = {
        "messages": [HumanMessage(content="Diagnose PostgreSQL performance from available read-only evidence.")],
        "current_intent": intent,
        "database_environment": _db_env(),
    }

    result = intent_validator(state)
    validated = result["current_intent"]

    assert validated["requires_clarification"] is False
    assert validated["primary_intent"] == "performance_diagnosis"
    assert validated["target_database"] == "db_agent"
    assert validated["missing_slots"] == []
    assert validated["next_action"] == "read_only_observe"
    assert "execution_plan" in validated["evidence_needed"]


def test_validator_treats_chinese_slow_sql_request_as_configured_readonly_diagnostic():
    intent = normalize_intent({}, "查询数据库中执行较慢的sql")
    state = {
        "messages": [HumanMessage(content="查询数据库中执行较慢的sql")],
        "current_intent": intent,
        "database_environment": _db_env(),
    }

    result = intent_validator(state)
    validated = result["current_intent"]

    assert validated["primary_intent"] == "performance_diagnosis"
    assert validated["target_environment"] == "dev"
    assert validated["target_database"] == "db_agent"
    assert validated["requires_clarification"] is False
    assert validated["missing_slots"] == []
    assert validated["next_action"] == "read_only_observe"


def test_validator_does_not_clarify_self_discoverable_slow_sql_sources():
    intent = normalize_intent(
        {
            "domain": "postgresql",
            "primary_intent": "performance_diagnosis",
            "candidate_intents": ["performance_diagnosis"],
            "confidence": 0.9,
            "goal": "Find the ten slowest SQL statements.",
            "user_language_summary": "数据库执行过的最慢的十条sql是什么",
            "operation_nature": "diagnostic",
            "target_environment": "unknown",
            "target_database": None,
            "target_objects": [],
            "input_artifacts": [],
            "missing_slots": [
                "target_environment",
                "target_database",
                "pg_stat_statements 扩展是否已安装并启用",
                "是否需要按总耗时、平均耗时或最大耗时排序",
            ],
            "risk_level": "low",
            "requires_clarification": True,
            "requires_approval": False,
            "requires_rollback_plan": False,
            "evidence_needed": [],
            "suggested_workflow": "performance_diagnosis_workflow",
            "next_action": "ask_clarification",
        },
        "数据库执行过的最慢的十条sql是什么",
    )
    state = {
        "messages": [HumanMessage(content="数据库执行过的最慢的十条sql是什么")],
        "current_intent": intent,
        "database_environment": _db_env(),
    }

    result = intent_validator(state)
    validated = result["current_intent"]

    assert validated["requires_clarification"] is False
    assert validated["missing_slots"] == []
    assert "top_queries" in validated["evidence_needed"]
    assert validated["next_action"] == "read_only_observe"


def test_validator_treats_current_database_listing_as_configured_readonly_query():
    intent = normalize_intent({}, "当前环境存在哪些数据库？")
    state = {
        "messages": [HumanMessage(content="当前环境存在哪些数据库？")],
        "current_intent": intent,
        "database_environment": _db_env(),
    }

    result = intent_validator(state)
    validated = result["current_intent"]

    assert validated["primary_intent"] == "read_only_analysis"
    assert validated["requires_clarification"] is False
    assert validated["missing_slots"] == []


def test_validator_clears_pending_clarification_when_model_resolves_context():
    intent = normalize_intent(
        {
            "domain": "postgresql",
            "primary_intent": "performance_diagnosis",
            "candidate_intents": ["performance_diagnosis"],
            "confidence": 0.9,
            "goal": "Diagnose PostgreSQL performance with configured scope.",
            "user_language_summary": "Diagnose PostgreSQL performance with configured scope.",
            "operation_nature": "diagnostic",
            "target_environment": "dev",
            "target_database": "db_agent",
            "target_objects": [],
            "input_artifacts": [],
            "missing_slots": [],
            "risk_level": "low",
            "requires_clarification": False,
            "requires_approval": False,
            "requires_rollback_plan": False,
            "evidence_needed": ["top_queries"],
            "suggested_workflow": "performance_diagnosis_workflow",
            "next_action": "read_only_observe",
        },
        "Diagnose PostgreSQL performance with configured scope.",
    )
    state = {
        "messages": [HumanMessage(content="Diagnose PostgreSQL performance with configured scope.")],
        "current_intent": intent,
        "pending_clarification": {
            "id": "clarify-1",
            "questions": ["What scope should be used?"],
            "missing_slots": ["time_range"],
            "reason": "missing",
            "status": "pending",
        },
        "database_environment": _db_env(),
    }

    result = intent_validator(state)
    validated = result["current_intent"]

    assert validated["requires_clarification"] is False
    assert result["pending_clarification"] is None
    assert validated["primary_intent"] == "performance_diagnosis"
    assert validated["next_action"] == "read_only_observe"


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


def test_validator_does_not_ask_user_for_rollback_plan_before_approval():
    intent = normalize_intent({}, "开始优化这条语句吧，创建索引")
    state = {
        "messages": [HumanMessage(content="开始优化这条语句吧，创建索引")],
        "current_intent": intent,
        "database_environment": _db_env(),
    }

    result = intent_validator(state)
    validated = result["current_intent"]

    assert validated["operation_nature"] == "schema_change"
    assert validated["requires_approval"] is True
    assert validated["requires_rollback_plan"] is True
    assert "rollback_plan" not in validated["missing_slots"]
    assert validated["requires_clarification"] is False
    assert validated["next_action"] == "request_approval"


def test_clarification_gate_returns_message_and_pending_request():
    intent = normalize_intent({}, "数据库最近很慢，帮我看看")
    intent["missing_slots"] = ["target_environment", "sql_or_symptom"]
    intent["requires_clarification"] = True
    intent["risk_level"] = "low"

    result = clarification_gate({"current_intent": intent})

    assert result["pending_clarification"]["status"] == "pending"
    assert len(result["pending_clarification"]["questions"]) == 2
    assert result["messages"]
