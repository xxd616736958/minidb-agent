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

logger = logging.getLogger(__name__)


INTENT_ANALYZER_SYSTEM_PROMPT = """\
You are the task-understanding layer for a PostgreSQL management agent.

Your job is to turn the latest user request into one JSON object that follows
the DBTaskIntent schema. Do not execute SQL. Do not invent database names,
table names, column names, metrics, or environments. If information is missing,
put it in missing_slots.

Design principles:
- Use coarse intent families, not fine-grained action names.
- Allow multiple candidate_intents when the request is mixed or ambiguous.
- Classify risk conservatively for database operations.
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
            "表",
            "索引",
            "慢查询",
            "查询",
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
        response = llm.invoke(
            [
                SystemMessage(content=INTENT_ANALYZER_SYSTEM_PROMPT),
                HumanMessage(content=user_content),
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
    return {
        "current_intent": intent,
        "intent_history": [intent],
        "selected_workflow": intent.get("suggested_workflow"),
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
    return bool(
        re.search(r"\bselect\b|\bexplain\b|\bfrom\b", text, re.IGNORECASE)
        or _contains_any(text, ("慢", "卡", "超时", "timeout", "slow"))
    )


def _append_missing(intent: DBTaskIntent, slot: str) -> None:
    if slot not in intent["missing_slots"]:
        intent["missing_slots"].append(slot)


def intent_validator(state: AgentState) -> dict[str, Any]:
    """Validate intent completeness and apply deterministic database safety rules."""
    intent = state.get("current_intent")
    if not intent:
        return {}

    intent = dict(intent)
    user_content = _latest_user_content(state)
    text = " ".join(
        [
            user_content,
            intent.get("goal", ""),
            " ".join(str(item) for item in intent.get("input_artifacts", [])),
        ]
    )
    db_context = _is_database_context(intent, text)

    if intent["domain"] == "postgresql":
        if intent["target_environment"] == "unknown":
            _append_missing(intent, "target_environment")

        if intent["primary_intent"] in {"unknown_or_mixed", "read_only_analysis"}:
            if not intent["target_objects"] and not intent["input_artifacts"]:
                _append_missing(intent, "target_objects_or_sql")

        if intent["primary_intent"] == "performance_diagnosis":
            if not _has_sql_or_symptom(intent, text):
                _append_missing(intent, "sql_or_symptom")
            if not re.search(r"\b(select|explain)\b", text, re.IGNORECASE):
                _append_missing(intent, "time_range")
            if not intent["evidence_needed"]:
                intent["evidence_needed"] = [
                    "slow_query_sample",
                    "execution_plan",
                    "schema_summary",
                    "index_summary",
                    "row_count_or_statistics",
                ]

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
        _append_missing(intent, "rollback_plan")

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

    return {
        "current_intent": intent,
        "selected_workflow": intent["suggested_workflow"],
    }


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

    return {
        "pending_clarification": request,
        "messages": [AIMessage(content="\n".join(lines))],
    }


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
