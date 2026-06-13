"""Task understanding and intent modeling for PostgreSQL management tasks."""

from __future__ import annotations

import json
import logging
import re
import uuid
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from agent.llm_factory import create_llm_no_tools
from agent.state import AgentState, ClarificationRequest, DBTaskIntent
from collaboration.manager import CollaborationManager
from memory.consolidator import consolidate_explicit_memory_request
from memory.schema import build_memory_query
from memory.store import get_memory_store

logger = logging.getLogger(__name__)


INTENT_ANALYZER_SYSTEM_PROMPT = """\
You are the task-understanding layer for a PostgreSQL management agent.

You receive a JSON context packet, not an isolated sentence. The packet
contains the latest user message, recent conversation, configured PostgreSQL
target, runtime policy, pending clarification, and current intent. Turn that
context into one JSON object that follows the DBTaskIntent schema. Do not
execute SQL. Do not invent database names, table names, column names, metrics,
or environments. If information is truly missing, put it in missing_slots.

Design principles:
- Use coarse intent families, not fine-grained action names.
- Allow multiple candidate_intents when the request is mixed or ambiguous.
- Classify risk conservatively for database operations.
- Interpret the latest message in conversation context. If it answers a
  pending clarification, merge it into the existing intent instead of treating
  it as a new unrelated task.
- Prefer the configured PostgreSQL target when the user omitted environment or
  database. Do not ask for information that already exists in the context packet.
- For low-risk read-only or diagnostic PostgreSQL tasks, proceed with the
  configured target and explicit assumptions. Optional scope such as target
  object, time window, metric range, threshold, or sample SQL should not block
  the first read-only observation unless no useful safe observation can be made.
- Do not ask users whether PostgreSQL diagnostic sources exist, such as
  pg_stat_statements, pg_stat_activity, schema catalogs, server version, or
  lock views. The agent should inspect available read-only sources itself and
  report limitations.
- Do not ask users to choose a default diagnostic sort metric. For "slowest SQL"
  requests, start with pg_stat_statements ordered by total time / mean time when
  available, then explain the metric used.
- Ask clarification only when the target connection is unavailable, mutation
  scope is unsafe or ambiguous, a destructive operation lacks safety filters, or
  the user's goal cannot be inferred from the conversation.
- Documentation/report tasks should use primary_intent="documentation" unless
  they require real database evidence; then include read_only_analysis too.
- If the task is not database-related, set domain to documentation/code/general
  and keep risk low unless the text itself implies a risky operation.

Allowed primary_intent values:
- read_only_analysis
- performance_diagnosis
- schema_change
- data_change
- permission_admin
- backup_restore
- documentation
- general_question
- unknown_or_mixed

Allowed suggested_workflow values:
- read_only_analysis_workflow
- performance_diagnosis_workflow
- schema_change_workflow
- data_change_workflow
- permission_admin_workflow
- backup_restore_workflow
- documentation_workflow
- general_workflow
- unknown_or_mixed_workflow

Risk guide:
- low: SELECT, EXPLAIN, metadata inspection, writing docs without mutation
- medium: generating change SQL without executing it, non-prod index advice
- high: CREATE INDEX, ALTER TABLE, UPDATE, DELETE, GRANT/REVOKE
- critical: DROP, TRUNCATE, unconditional UPDATE/DELETE, irreversible prod ops

Return ONLY valid JSON with these fields:
{
  "domain": "postgresql|documentation|code|general|unknown",
  "primary_intent": "string",
  "candidate_intents": ["string"],
  "confidence": 0.0,
  "goal": "string",
  "user_language_summary": "string",
  "operation_nature": "read_only|diagnostic|write_data|schema_change|permission_change|backup_restore|documentation|unknown",
  "target_environment": "production|staging|dev|local|unknown",
  "target_database": null,
  "target_objects": [],
  "input_artifacts": [],
  "output_contract": {},
  "missing_slots": [],
  "assumptions": [],
  "constraints": [],
  "risk_level": "low|medium|high|critical|unknown",
  "requires_clarification": false,
  "requires_approval": false,
  "requires_rollback_plan": false,
  "evidence_needed": [],
  "suggested_workflow": "string",
  "next_action": "ask_clarification|plan|read_only_observe|request_approval|decline"
}
"""


_VALID_DOMAINS = {"postgresql", "documentation", "code", "general", "unknown"}
_VALID_OPERATION_NATURES = {
    "read_only",
    "diagnostic",
    "write_data",
    "schema_change",
    "permission_change",
    "backup_restore",
    "documentation",
    "unknown",
}
_VALID_ENVIRONMENTS = {"production", "staging", "dev", "local", "unknown"}
_VALID_RISKS = {"low", "medium", "high", "critical", "unknown"}
_VALID_NEXT_ACTIONS = {
    "ask_clarification",
    "plan",
    "read_only_observe",
    "request_approval",
    "decline",
}

_INTENT_TO_WORKFLOW = {
    "read_only_analysis": "read_only_analysis_workflow",
    "performance_diagnosis": "performance_diagnosis_workflow",
    "schema_change": "schema_change_workflow",
    "data_change": "data_change_workflow",
    "permission_admin": "permission_admin_workflow",
    "backup_restore": "backup_restore_workflow",
    "documentation": "documentation_workflow",
    "general_question": "general_workflow",
    "unknown_or_mixed": "unknown_or_mixed_workflow",
}

_RISK_RANK = {
    "unknown": 0,
    "low": 1,
    "medium": 2,
    "high": 3,
    "critical": 4,
}


def _latest_user_content(state: AgentState) -> str:
    for msg in reversed(state.get("messages", [])):
        if isinstance(msg, HumanMessage) or getattr(msg, "type", None) == "human":
            return str(msg.content) if hasattr(msg, "content") else str(msg)
    return ""


def _extract_json(raw: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        pass

    match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", raw)
    if match:
        try:
            parsed = json.loads(match.group(1))
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            pass

    match = re.search(r"\{[\s\S]*\}", raw)
    if match:
        try:
            parsed = json.loads(match.group(0))
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            pass

    return {}


def _as_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if item is not None]
    return [str(value)]


def _as_dict_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _coerce_enum(value: Any, valid: set[str], default: str) -> str:
    text = str(value or "").strip()
    return text if text in valid else default


def _coerce_confidence(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, number))


def _fallback_intent(user_content: str) -> DBTaskIntent:
    lowered = user_content.lower()
    domain = "postgresql" if any(
        token in lowered
        for token in (
            "postgres",
            "postgresql",
            "database",
            "数据库",
            "sql",
        )
    ) else "general"

    primary = "unknown_or_mixed" if domain == "postgresql" else "general_question"
    operation = "unknown" if domain == "postgresql" else "read_only"
    workflow = _INTENT_TO_WORKFLOW.get(primary, "unknown_or_mixed_workflow")

    return {
        "id": f"intent-{uuid.uuid4().hex[:12]}",
        "domain": domain,
        "primary_intent": primary,
        "candidate_intents": [primary],
        "confidence": 0.2,
        "goal": user_content,
        "user_language_summary": user_content,
        "operation_nature": operation,
        "target_environment": "unknown",
        "target_database": None,
        "target_objects": [],
        "input_artifacts": [],
        "output_contract": {},
        "missing_slots": ["task_scope"] if domain == "postgresql" else [],
        "assumptions": [],
        "constraints": [],
        "risk_level": "unknown" if domain == "postgresql" else "low",
        "requires_clarification": domain == "postgresql",
        "requires_approval": False,
        "requires_rollback_plan": False,
        "evidence_needed": [],
        "suggested_workflow": workflow,
        "next_action": "ask_clarification" if domain == "postgresql" else "plan",
    }


def _configured_database_environment(state: AgentState) -> dict[str, Any]:
    env = state.get("database_environment") or {}
    return env if isinstance(env, dict) else {}


def _default_environment_from_state(state: AgentState) -> str:
    env = str(_configured_database_environment(state).get("environment_name") or "unknown").strip()
    if env in _VALID_ENVIRONMENTS:
        return env
    return "unknown"


def _default_database_from_state(state: AgentState) -> str | None:
    database = _configured_database_environment(state).get("target_database")
    return str(database) if database else None


def _append_assumption(intent: DBTaskIntent, assumption: str) -> None:
    if assumption and assumption not in intent["assumptions"]:
        intent["assumptions"].append(assumption)


def _remove_missing(intent: DBTaskIntent, *slots: str) -> None:
    blocked = set(slots)
    intent["missing_slots"] = [slot for slot in intent.get("missing_slots", []) if slot not in blocked]


def _apply_configured_context(intent: DBTaskIntent, state: AgentState) -> None:
    env = _default_environment_from_state(state)
    database = _default_database_from_state(state)
    if intent["target_environment"] == "unknown" and env != "unknown":
        intent["target_environment"] = env  # type: ignore[typeddict-item]
        _remove_missing(intent, "target_environment")
        _append_assumption(intent, f"未显式指定环境，使用当前配置环境 {env}。")
    if not intent.get("target_database") and database:
        intent["target_database"] = database
        _remove_missing(intent, "target_database")
        _append_assumption(intent, f"未显式指定数据库，使用当前连接数据库 {database}。")


def _message_role(message: Any) -> str:
    role = getattr(message, "type", None)
    if isinstance(message, dict):
        role = message.get("role") or message.get("type") or role
    if isinstance(message, HumanMessage) or role in {"human", "user"}:
        return "user"
    if isinstance(message, AIMessage) or role in {"ai", "assistant"}:
        return "assistant"
    if isinstance(message, SystemMessage) or role == "system":
        return "system"
    return str(role or "unknown")


def _message_content(message: Any) -> str:
    content = getattr(message, "content", None)
    if content is None and isinstance(message, dict):
        content = message.get("content")
    if isinstance(content, list):
        return json.dumps(content, ensure_ascii=False, default=str)
    return str(content or "")


def _conversation_packet(state: AgentState, latest_user_message: str) -> dict[str, Any]:
    conversation: list[dict[str, str]] = []
    for message in state.get("messages", [])[-10:]:
        content = _message_content(message).strip()
        if content:
            conversation.append(
                {
                    "role": _message_role(message),
                    "content": content[:4000],
                }
            )

    return {
        "latest_user_message": latest_user_message,
        "conversation": conversation,
        "configured_database_environment": _configured_database_environment(state),
        "runtime_policy": state.get("runtime_policy") or {},
        "pending_clarification": state.get("pending_clarification"),
        "current_intent": state.get("current_intent"),
        "confirmed_context": state.get("confirmed_context") or {},
    }


def _has_configured_target(state: AgentState) -> bool:
    return _default_environment_from_state(state) != "unknown" and bool(_default_database_from_state(state))


def _is_read_only_or_diagnostic(intent: DBTaskIntent) -> bool:
    if intent.get("operation_nature") in {"read_only", "diagnostic"}:
        return True
    if intent.get("primary_intent") in {"read_only_analysis", "performance_diagnosis"}:
        return True
    return False


def _relax_read_only_missing_slots(intent: DBTaskIntent, state: AgentState) -> None:
    if intent.get("domain") != "postgresql" or not _is_read_only_or_diagnostic(intent):
        return

    removable = [
        "target_objects",
        "target_objects_or_sql",
        "sql_or_symptom",
        "time_range",
        "metric_scope",
        "threshold",
        "task_scope",
        "sort_metric",
        "sort_by",
        "order_by",
        "diagnostic_source",
        "pg_stat_statements",
        "pg_stat_statements_enabled",
        "pg_stat_activity",
        "database_version",
        "version",
        "source",
    ]
    removable.extend(
        slot
        for slot in intent.get("missing_slots", [])
        if any(
            token in slot.lower()
            for token in (
                "pg_stat_statements",
                "pg_stat_activity",
                "sort",
                "排序",
                "扩展",
                "extension",
                "版本",
                "version",
                "来源",
                "source",
            )
        )
    )
    if _default_environment_from_state(state) != "unknown":
        removable.append("target_environment")
    if _default_database_from_state(state):
        removable.append("target_database")

    before = set(intent.get("missing_slots", []))
    _remove_missing(intent, *removable)
    if before != set(intent.get("missing_slots", [])) and _has_configured_target(state):
        _append_assumption(
            intent,
            "Using the configured PostgreSQL target and safe read-only discovery for unspecified diagnostic scope.",
        )

    if not intent.get("missing_slots"):
        intent["requires_clarification"] = False
        if intent.get("next_action") == "ask_clarification":
            intent["next_action"] = "read_only_observe"


def normalize_intent(raw_intent: dict[str, Any], user_content: str) -> DBTaskIntent:
    """Normalize model output into a complete DBTaskIntent."""
    fallback = _fallback_intent(user_content)

    primary = str(raw_intent.get("primary_intent") or fallback["primary_intent"]).strip()
    if not primary:
        primary = fallback["primary_intent"]

    workflow = str(raw_intent.get("suggested_workflow") or "").strip()
    if not workflow:
        workflow = _INTENT_TO_WORKFLOW.get(primary, fallback["suggested_workflow"])

    candidate_intents = _as_str_list(raw_intent.get("candidate_intents")) or [primary]
    if primary not in candidate_intents:
        candidate_intents.insert(0, primary)

    output_contract = raw_intent.get("output_contract")
    if not isinstance(output_contract, dict):
        output_contract = {}

    return {
        "id": str(raw_intent.get("id") or fallback["id"]),
        "domain": _coerce_enum(raw_intent.get("domain"), _VALID_DOMAINS, fallback["domain"]),
        "primary_intent": primary,
        "candidate_intents": candidate_intents,
        "confidence": _coerce_confidence(raw_intent.get("confidence", fallback["confidence"])),
        "goal": str(raw_intent.get("goal") or fallback["goal"]),
        "user_language_summary": str(
            raw_intent.get("user_language_summary") or fallback["user_language_summary"]
        ),
        "operation_nature": _coerce_enum(
            raw_intent.get("operation_nature"),
            _VALID_OPERATION_NATURES,
            fallback["operation_nature"],
        ),
        "target_environment": _coerce_enum(
            raw_intent.get("target_environment"),
            _VALID_ENVIRONMENTS,
            fallback["target_environment"],
        ),
        "target_database": (
            str(raw_intent["target_database"])
            if raw_intent.get("target_database") is not None
            else None
        ),
        "target_objects": _as_dict_list(raw_intent.get("target_objects")),
        "input_artifacts": _as_dict_list(raw_intent.get("input_artifacts")),
        "output_contract": output_contract,
        "missing_slots": _as_str_list(raw_intent.get("missing_slots")),
        "assumptions": _as_str_list(raw_intent.get("assumptions")),
        "constraints": _as_str_list(raw_intent.get("constraints")),
        "risk_level": _coerce_enum(raw_intent.get("risk_level"), _VALID_RISKS, fallback["risk_level"]),
        "requires_clarification": bool(raw_intent.get("requires_clarification", False)),
        "requires_approval": bool(raw_intent.get("requires_approval", False)),
        "requires_rollback_plan": bool(raw_intent.get("requires_rollback_plan", False)),
        "evidence_needed": _as_str_list(raw_intent.get("evidence_needed")),
        "suggested_workflow": workflow,
        "next_action": _coerce_enum(
            raw_intent.get("next_action"),
            _VALID_NEXT_ACTIONS,
            fallback["next_action"],
        ),
    }


def intent_analyzer(state: AgentState) -> dict[str, Any]:
    """Analyze the latest user request into a structured task intent."""
    user_content = _latest_user_content(state)
    if not user_content:
        return {}

    logger.info("Analyzing task intent")

    try:
        llm = create_llm_no_tools(temperature=0.0, max_tokens=1200)
        packet = _conversation_packet(state, user_content)
        response = llm.invoke(
            [
                SystemMessage(content=INTENT_ANALYZER_SYSTEM_PROMPT),
                HumanMessage(content=json.dumps(packet, ensure_ascii=False, indent=2, default=str)),
            ]
        )
        raw = str(response.content) if hasattr(response, "content") else str(response)
        parsed = _extract_json(raw)
        if not parsed:
            logger.warning("Intent analyzer returned non-JSON output; using fallback")
    except Exception as e:
        logger.error(f"Intent analyzer failed: {e}")
        parsed = {}

    intent = normalize_intent(parsed, user_content)
    _apply_configured_context(intent, state)
    _relax_read_only_missing_slots(intent, state)
    explicit_memory_updates = consolidate_explicit_memory_request(state, user_content)
    feedback_updates = CollaborationManager(state).feedback_update_from_text(
        user_content,
        target_ref=intent.get("id"),
    )
    return {
        "current_intent": intent,
        "intent_history": [intent],
        "selected_workflow": intent.get("suggested_workflow"),
        **explicit_memory_updates,
        **feedback_updates,
    }


def _upgrade_risk(intent: DBTaskIntent, minimum: str) -> None:
    if _RISK_RANK[minimum] > _RISK_RANK[intent["risk_level"]]:
        intent["risk_level"] = minimum


def _contains_any(text: str, words: tuple[str, ...]) -> bool:
    for word in words:
        if word.isascii():
            if re.search(rf"\b{re.escape(word)}\b", text, re.IGNORECASE):
                return True
        elif word in text:
            return True
    return False


def _is_database_context(intent: DBTaskIntent, text: str) -> bool:
    return intent.get("domain") == "postgresql" or _contains_any(
        text,
        (
            "postgres",
            "postgresql",
            "database",
            "sql",
            "query",
            "table",
            "index",
            "数据库",
            "查询",
            "表",
            "索引",
            "慢查询",
        ),
    )


def _looks_like_sql_mutation(text: str) -> bool:
    return bool(
        re.search(r"\bdelete\s+from\b", text, re.IGNORECASE)
        or re.search(r"\bupdate\s+\S+\s+set\b", text, re.IGNORECASE)
        or re.search(r"\binsert\s+into\b", text, re.IGNORECASE)
        or re.search(r"\bmerge\s+into\b", text, re.IGNORECASE)
    )


def _has_sql_or_symptom(intent: DBTaskIntent, text: str) -> bool:
    if intent.get("input_artifacts") or intent.get("target_objects"):
        return True
    if _contains_any(
        text,
        (
            "slow",
            "latency",
            "performance",
            "health",
            "locks",
            "active queries",
            "top queries",
            "慢",
            "慢查询",
            "执行较慢",
            "性能",
            "健康",
            "锁",
            "最慢",
        ),
    ):
        return True
    return bool(re.search(r"\bselect\b|\bexplain\b|\bfrom\b", text, re.IGNORECASE))


def _looks_like_performance_or_health_request(text: str) -> bool:
    return _contains_any(
        text,
        (
            "slow",
            "latency",
            "performance",
            "pg_stat_statements",
            "health",
            "locks",
            "慢",
            "慢查询",
            "执行较慢",
            "性能",
            "健康",
            "锁",
            "连接数",
            "活跃查询",
        ),
    )


def _looks_like_read_only_discovery_request(text: str) -> bool:
    return _contains_any(
        text,
        (
            "select",
            "explain",
            "show",
            "describe",
            "schema",
            "table",
            "tables",
            "index",
            "indexes",
            "version",
            "databases",
            "current database",
            "有哪些",
            "查看",
            "检查",
            "查询",
            "列出",
            "数据库",
            "表",
            "索引",
            "版本",
            "当前环境",
        ),
    )


def _classify_unknown_read_only_request(intent: DBTaskIntent, text: str) -> None:
    if intent.get("domain") != "postgresql" or intent.get("primary_intent") != "unknown_or_mixed":
        return
    if _looks_like_performance_or_health_request(text):
        intent["primary_intent"] = "performance_diagnosis"
        intent["candidate_intents"] = ["performance_diagnosis", "read_only_analysis"]
        intent["operation_nature"] = "diagnostic"
    elif _looks_like_read_only_discovery_request(text):
        intent["primary_intent"] = "read_only_analysis"
        intent["candidate_intents"] = ["read_only_analysis"]
        intent["operation_nature"] = "read_only"
    else:
        return
    if intent.get("risk_level") == "unknown":
        intent["risk_level"] = "low"


def _append_missing(intent: DBTaskIntent, slot: str) -> None:
    if slot not in intent["missing_slots"]:
        intent["missing_slots"].append(slot)


def _apply_safety_memories(intent: DBTaskIntent, state: AgentState) -> None:
    """Merge active SafetyMemory records into the current intent constraints."""
    query_state = {**state, "current_intent": intent}
    memories = get_memory_store().search(build_memory_query(query_state), limit=8)

    for memory in memories:
        if memory.get("kind") != "prohibition":
            continue
        summary = str(memory.get("summary", "")).strip()
        if not summary:
            continue
        constraint = f"SafetyMemory: {summary}"
        if constraint not in intent["constraints"]:
            intent["constraints"].append(constraint)


def intent_validator(state: AgentState) -> dict[str, Any]:
    """Validate intent completeness and apply deterministic database safety rules."""
    intent = state.get("current_intent")
    if not intent:
        return {}

    intent = dict(intent)
    _apply_safety_memories(intent, state)
    user_content = _latest_user_content(state)
    _apply_configured_context(intent, state)
    text = " ".join(
        [
            user_content,
            intent.get("goal", ""),
            " ".join(str(item) for item in intent.get("input_artifacts", [])),
        ]
    )
    db_context = _is_database_context(intent, text)
    _classify_unknown_read_only_request(intent, text)

    if intent["domain"] == "postgresql":
        if intent["target_environment"] == "unknown":
            _append_missing(intent, "target_environment")

        if intent["primary_intent"] == "unknown_or_mixed":
            if not intent["target_objects"] and not intent["input_artifacts"]:
                _append_missing(intent, "target_objects_or_sql")

        if intent["primary_intent"] == "performance_diagnosis":
            if not _has_sql_or_symptom(intent, text):
                _append_missing(intent, "sql_or_symptom")
            if not _looks_like_performance_or_health_request(text) and not re.search(r"\b(select|explain)\b", text, re.IGNORECASE):
                _append_missing(intent, "time_range")
            if not intent["evidence_needed"]:
                intent["evidence_needed"] = [
                    "top_queries",
                    "active_queries",
                    "slow_query_sample",
                    "execution_plan",
                    "schema_summary",
                    "index_summary",
                    "row_count_or_statistics",
                ]
            _append_assumption(
                intent,
                "For read-only performance diagnostics, inspect available PostgreSQL statistics first and choose safe default metrics instead of asking the user to preselect them.",
            )

    if db_context and _contains_any(text, ("drop", "truncate", "删表", "删除表", "清空表", "截断")):
        intent["domain"] = "postgresql"
        intent["operation_nature"] = "schema_change"
        _upgrade_risk(intent, "critical")

    if db_context and (
        _contains_any(text, ("delete", "update", "insert", "merge", "删除数据", "清理数据", "清理掉", "更新数据", "插入数据"))
        or _looks_like_sql_mutation(text)
    ):
        intent["domain"] = "postgresql"
        intent["operation_nature"] = "write_data"
        _upgrade_risk(intent, "high")
        intent["requires_rollback_plan"] = True
        if not re.search(r"\bwhere\b", text, re.IGNORECASE):
            _upgrade_risk(intent, "critical")
            _append_missing(intent, "where_condition_or_safety_filter")

    if db_context and _contains_any(text, ("alter", "create index", "reindex", "建索引", "创建索引", "改表", "修改表结构")):
        intent["domain"] = "postgresql"
        intent["operation_nature"] = "schema_change"
        _upgrade_risk(intent, "high")
        intent["requires_rollback_plan"] = True

    if db_context and _contains_any(text, ("grant", "revoke", "授权", "撤销权限")):
        intent["domain"] = "postgresql"
        intent["operation_nature"] = "permission_change"
        _upgrade_risk(intent, "high")

    if intent["risk_level"] in {"high", "critical"}:
        intent["requires_approval"] = True
        if intent["target_environment"] == "unknown":
            _append_missing(intent, "target_environment")

    if intent["requires_rollback_plan"]:
        _append_assumption(
            intent,
            "变更任务由系统生成回滚、影响和验证说明，并在执行前随审批卡一起让用户确认。",
        )

    _relax_read_only_missing_slots(intent, state)

    if intent["missing_slots"]:
        intent["requires_clarification"] = True
        intent["next_action"] = "ask_clarification"
    elif intent["requires_approval"]:
        intent["next_action"] = "request_approval"
    elif intent["domain"] == "postgresql" and intent["operation_nature"] in {"read_only", "diagnostic"}:
        intent["next_action"] = "read_only_observe"
    else:
        intent["next_action"] = "plan"

    if intent["primary_intent"] in _INTENT_TO_WORKFLOW:
        intent["suggested_workflow"] = _INTENT_TO_WORKFLOW[intent["primary_intent"]]
    elif not intent.get("suggested_workflow"):
        intent["suggested_workflow"] = "unknown_or_mixed_workflow"

    update = {
        "current_intent": intent,
        "selected_workflow": intent["suggested_workflow"],
    }
    if state.get("pending_clarification") and not intent.get("requires_clarification"):
        update["pending_clarification"] = None
    update.update(CollaborationManager(state).task_card_update(intent))
    return update


def _question_for_slot(slot: str) -> str:
    questions = {
        "target_environment": "这个任务要针对哪个环境执行？例如 production、staging、dev 或 local。",
        "target_objects": "请指定涉及的数据库对象，例如表名、索引名、视图名或 schema。",
        "target_objects_or_sql": "请提供目标表/索引/SQL，或说明要分析的数据库对象范围。",
        "sql_or_symptom": "请提供慢 SQL、接口名、报错信息或你观察到的性能症状。",
        "time_range": "请提供问题发生的时间范围，方便限定慢查询和指标窗口。",
        "where_condition_or_safety_filter": "请提供安全过滤条件，例如 WHERE 条件、时间范围或主键范围。",
        "rollback_plan": "这个变更是否需要回滚方案？如果已有要求，请说明备份或回滚策略。",
        "task_scope": "请补充这个数据库任务的具体目标和允许范围。",
    }
    return questions.get(slot, f"请补充缺失信息：{slot}。")


def _build_clarification(intent: DBTaskIntent) -> ClarificationRequest:
    missing_slots = intent.get("missing_slots", [])[:3]
    return {
        "id": f"clarify-{uuid.uuid4().hex[:12]}",
        "questions": [_question_for_slot(slot) for slot in missing_slots],
        "missing_slots": missing_slots,
        "reason": "当前数据库任务信息不足，继续规划或执行前需要先确认关键上下文。",
        "status": "pending",
    }


def clarification_gate(state: AgentState) -> dict[str, Any]:
    """Stop before planning when intent is too ambiguous or unsafe."""
    intent = state.get("current_intent")
    if not intent or not intent.get("requires_clarification"):
        return {"pending_clarification": None}

    request = _build_clarification(intent)
    lines = [
        "我需要先确认几个信息，避免误操作数据库：",
        "",
    ]
    for idx, question in enumerate(request["questions"], start=1):
        lines.append(f"{idx}. {question}")
    lines.extend(
        [
            "",
            f"当前理解：{intent.get('user_language_summary') or intent.get('goal')}",
            f"风险等级：{intent.get('risk_level')}",
        ]
    )

    update = {
        "pending_clarification": request,
        "messages": [AIMessage(content="\n".join(lines))],
    }
    update.update(CollaborationManager(state).clarification_update(request["id"], request["questions"]))
    return update


WORKFLOW_DESCRIPTIONS = {
    "read_only_analysis_workflow": [
        "确认只读范围和目标对象",
        "收集 schema、索引、统计信息或查询结果",
        "给出分析结论和后续建议",
    ],
    "performance_diagnosis_workflow": [
        "确认慢 SQL、接口、时间范围和环境",
        "只读收集执行计划、索引、行数和统计信息",
        "判断瓶颈并给出优化方案",
        "如需变更，先生成方案、影响范围和回滚计划",
    ],
    "schema_change_workflow": [
        "确认环境、目标对象和变更目标",
        "生成迁移 SQL 和回滚 SQL",
        "评估锁表、耗时和兼容性风险",
        "等待用户审批后再执行",
    ],
    "data_change_workflow": [
        "确认目标表、过滤条件、影响范围和环境",
        "先用 SELECT 预估影响行数",
        "生成变更 SQL、备份策略和回滚 SQL",
        "等待用户审批后再执行",
    ],
    "permission_admin_workflow": [
        "确认角色、对象、权限范围和环境",
        "检查现有授权",
        "生成最小权限变更方案",
        "等待用户审批后再执行",
    ],
    "backup_restore_workflow": [
        "确认备份或恢复目标、时间点和环境",
        "检查影响范围和可用备份",
        "生成执行步骤和回退路径",
        "等待用户审批后再执行",
    ],
    "documentation_workflow": [
        "确认文档类型、读者和格式",
        "收集必要证据或上下文",
        "生成结构化文档",
        "根据用户反馈修订",
    ],
    "general_workflow": [
        "理解用户目标",
        "按需要规划和执行",
        "总结结果",
    ],
    "unknown_or_mixed_workflow": [
        "澄清任务范围和优先级",
        "拆分为可执行子任务",
        "按风险从低到高推进",
    ],
}


def workflow_planner(state: AgentState) -> dict[str, Any]:
    """Attach workflow guidance for the downstream task planner."""
    intent = state.get("current_intent")
    if not intent:
        return {}

    workflow = intent.get("suggested_workflow") or "unknown_or_mixed_workflow"
    steps = WORKFLOW_DESCRIPTIONS.get(workflow, WORKFLOW_DESCRIPTIONS["unknown_or_mixed_workflow"])
    return {
        "selected_workflow": workflow,
        "confirmed_context": {
            **(state.get("confirmed_context") or {}),
            "workflow_steps": steps,
        },
    }
