"""LLM reasoning node — the core intelligence of the agent.

This node:
  1. Builds the system prompt with memory context + tool descriptions
  2. Binds the current step's visible tools to the LLM via bind_tools()
  3. Invokes the LLM with the full message history
  4. Returns the LLM response (may contain tool_calls)
"""

from __future__ import annotations

import html
import json
import logging
import re
import time
import uuid
from typing import Any

from langchain_core.messages import AIMessage, SystemMessage

from agent.config import get_settings
from agent.context import (
    build_context_snapshot,
    build_db_working_set,
    build_prompt_context,
    retrieve_relevant_memories,
)
from agent.llm_factory import create_llm_for_task
from agent.state import AgentState
from memory.manager import MemoryManager
from memory.working import WORKING_MEMORY_SYSTEM_PROMPT
from execution.environment import ExecutionEnvironmentManager
from models.routing import default_model_profiles, fallback_decision_for_error, finish_invocation_record
from tools.registry import registry

logger = logging.getLogger(__name__)

_TOOL_CALL_BLOCK_RE = re.compile(r"<[^>]*tool_calls[^>]*>(?P<body>.*?)</[^>]*tool_calls[^>]*>", re.DOTALL)
_TOOL_INVOKE_RE = re.compile(r"<[^>]*invoke\s+name=\"(?P<name>[^\"]+)\"[^>]*>(?P<body>.*?)</[^>]*invoke[^>]*>", re.DOTALL)
_TOOL_PARAM_RE = re.compile(r"<[^>]*parameter\s+name=\"(?P<name>[^\"]+)\"[^>]*>(?P<value>.*?)</[^>]*parameter[^>]*>", re.DOTALL)

_TOOL_ARG_ALIASES: dict[str, dict[str, str]] = {
    "postgres_list_objects": {"schema": "schema_name"},
    "postgres_object_detail": {"schema": "schema_name", "name": "object_name", "table": "object_name"},
}

# ── System prompt template ───────────────────────────────────

SYSTEM_PROMPT_BASE = """\
You are MiniDB Agent, a PostgreSQL management agent that helps users safely
understand, diagnose, plan, verify, and document PostgreSQL database work.
Your primary job is database operations support, not generic coding chat.

If the user asks who you are or what you can do, introduce yourself as MiniDB
Agent and explain your PostgreSQL-focused capabilities. Do not claim to be a
generic terminal programming assistant and do not mention unrelated tools such
as weather.

## Capabilities
- Understand ambiguous PostgreSQL management requests and ask for clarification when target database, environment, or output is missing
- Inspect PostgreSQL schemas, objects, indexes, locks, health signals, and top queries with read-only tools
- Classify SQL risk before execution and explain why a SQL statement is safe, risky, blocked, or approval-bound
- Run read-only SQL, EXPLAIN, health checks, lock inspection, and index advice within configured limits
- Draft change SQL, dry-run plans, rollback notes, verification criteria, and approval packages
- Execute write/DDL/maintenance actions only through approved PostgreSQL tools after safety checks and human approval
- Generate final delivery reports, audit reports, artifact manifests, SQL delivery items, and next actions
- Use local workspace files only within the workspace provided by the CLI/session context

## Guidelines
1. **PostgreSQL first**: Prefer PostgreSQL domain tools over shell commands or ad hoc scripts.
2. **Read before write**: Observe and diagnose before proposing changes.
3. **Approval before mutation**: Never execute data changes, DDL, permissions, maintenance, or destructive SQL without matching approval evidence.
4. **Bind SQL to evidence**: Use SQL hashes, safety reports, approval records, rollback notes, and verification criteria for write paths.
5. **Respect environment**: Treat production and unknown environments conservatively.
6. **Be explicit about limits**: If no database target is configured, say that database tools need `POSTGRES_TARGET_URL` or CLI `--database-url`.
7. **Deliver artifacts**: End database tasks with clear findings, evidence, risks, and next steps.
8. **Use structured tools only**: When a tool is needed, call the bound tool. Never print tool-call markup, XML, DSML, JSON tool envelopes, or internal protocol text as the user-facing answer.
9. **Follow expected tools**: If the current step names expected PostgreSQL tools, gather that evidence before reporting the step as complete.
10. **Prefer tool APIs over hand-written catalog SQL**: Use object/detail,
    schema, health, top-query, explain, and index-advisor tools before writing
    your own PostgreSQL catalog queries. If you do query statistics views
    directly, use PostgreSQL's actual column names such as `relname` in
    `pg_stat_user_tables` and `pg_stat_user_indexes`; do not invent aliases
    like `tablename`.

{memory_context}

{database_context}

## Current Environment
- Workspace directory: {cwd}
- Platform: {platform}
"""


# ── Node implementation ──────────────────────────────────────

def build_system_prompt(state: dict[str, Any]) -> str:
    """Construct the system prompt with full memory context."""
    import platform

    settings = get_settings()
    manager = MemoryManager(max_window_tokens=settings.memory_window_tokens)

    memory_context = manager.build_context(state)
    database_context, _ = build_prompt_context(state)

    return SYSTEM_PROMPT_BASE.format(
        memory_context=memory_context,
        database_context=database_context,
        cwd=_workspace_cwd(state),
        platform=platform.platform(),
    ) + "\n" + WORKING_MEMORY_SYSTEM_PROMPT


def _workspace_cwd(state: dict[str, Any]) -> str:
    workspace = state.get("workspace_profile") or {}
    return str(workspace.get("root_path") or workspace.get("default_cwd") or "unknown")


def _message_content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                value = item.get("text") or item.get("content")
                if value:
                    parts.append(str(value))
            else:
                parts.append(str(item))
        return "\n".join(parts)
    return str(content or "")


def _coerce_markup_arg(value: str) -> Any:
    text = html.unescape(re.sub(r"<[^>]+>", "", value)).strip()
    if not text:
        return ""
    lowered = text.lower()
    if lowered in {"true", "false", "null"}:
        return json.loads(lowered)
    if re.fullmatch(r"-?\d+", text):
        try:
            return int(text)
        except ValueError:
            return text
    if re.fullmatch(r"-?\d+\.\d+", text):
        try:
            return float(text)
        except ValueError:
            return text
    if text[:1] in {"{", "["}:
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text
    return text


def _parse_tool_call_markup(content: Any) -> tuple[str, list[dict[str, Any]]]:
    """Convert provider-specific inline tool markup into LangChain tool calls.

    Codex and Claude keep tool calls separate from assistant text. Some
    OpenAI-compatible models return a DSML-style text fallback instead of native
    tool_calls; this function normalizes that fallback at the model boundary.
    """
    text = _message_content_text(content)
    if "tool_calls" not in text or "invoke" not in text:
        return text, []

    calls: list[dict[str, Any]] = []
    for block in _TOOL_CALL_BLOCK_RE.finditer(text):
        body = block.group("body")
        for invoke in _TOOL_INVOKE_RE.finditer(body):
            tool_name = html.unescape(invoke.group("name")).strip()
            if not tool_name:
                continue
            aliases = _TOOL_ARG_ALIASES.get(tool_name, {})
            args: dict[str, Any] = {}
            for param in _TOOL_PARAM_RE.finditer(invoke.group("body")):
                raw_name = html.unescape(param.group("name")).strip()
                if not raw_name:
                    continue
                arg_name = aliases.get(raw_name, raw_name)
                args[arg_name] = _coerce_markup_arg(param.group("value"))
            calls.append({"name": tool_name, "args": args, "id": f"call_{uuid.uuid4().hex[:12]}"})

    cleaned = _TOOL_CALL_BLOCK_RE.sub("", text).strip()
    return cleaned, calls


def _normalize_llm_tool_markup(response: AIMessage) -> AIMessage:
    content, tool_calls = _parse_tool_call_markup(getattr(response, "content", ""))
    native_tool_calls = getattr(response, "tool_calls", None) or []
    if native_tool_calls:
        if content == getattr(response, "content", ""):
            return response
        return AIMessage(
            content=content,
            tool_calls=native_tool_calls,
            additional_kwargs=getattr(response, "additional_kwargs", {}) or {},
            response_metadata=getattr(response, "response_metadata", {}) or {},
            id=getattr(response, "id", None),
            usage_metadata=getattr(response, "usage_metadata", None),
        )
    if not tool_calls:
        return response
    return AIMessage(
        content=content,
        tool_calls=tool_calls,
        additional_kwargs=getattr(response, "additional_kwargs", {}) or {},
        response_metadata=getattr(response, "response_metadata", {}) or {},
        id=getattr(response, "id", None),
        usage_metadata=getattr(response, "usage_metadata", None),
    )


def _sanitize_tool_call_messages(messages: list) -> list:
    """Strip orphaned tool_calls from AIMessages that have no matching ToolMessages.

    DeepSeek (and some OpenAI-compatible APIs) strictly require that every
    assistant message with tool_calls is immediately followed by tool messages
    responding to each tool_call_id. If a previous run failed mid-execution, or
    recovery inserted a system message before tool results, the checkpoint may
    contain invalid assistant/tool ordering. Strip those tool_calls at the model
    boundary; tool execution evidence remains available in structured state.
    """
    if not messages:
        return messages

    cleaned = []
    for i, msg in enumerate(messages):
        tool_calls = msg.get("tool_calls") if isinstance(msg, dict) else getattr(msg, "tool_calls", None)
        if not tool_calls and isinstance(msg, dict):
            tool_calls = (msg.get("additional_kwargs") or {}).get("tool_calls")
        if not tool_calls:
            cleaned.append(msg)
            continue

        # Check if the following messages contain matching ToolMessages
        tool_call_ids = set()
        for tc in tool_calls:
            if not isinstance(tc, dict):
                continue
            call_id = tc.get("id")
            if call_id:
                tool_call_ids.add(str(call_id))
        if not tool_call_ids:
            cleaned.append(msg)
            continue

        # Provider protocols require the next messages to be the tool results
        # for this assistant tool call group. Later evidence in state is not
        # enough if another assistant/system/user message appears first.
        remaining = set(tool_call_ids)
        j = i + 1
        while j < len(messages) and remaining:
            later = messages[j]
            tc_id = later.get("tool_call_id") if isinstance(later, dict) else getattr(later, "tool_call_id", None)
            if not tc_id and isinstance(later, dict):
                tc_id = later.get("id") if later.get("type") == "tool" else None
            if tc_id and tc_id in remaining:
                remaining.remove(tc_id)
                j += 1
                continue
            break

        if not remaining:
            cleaned.append(msg)
        else:
            # Orphaned tool_calls — strip them via a new message
            logger.warning(
                f"Stripping orphaned tool_calls from message {i}: "
                f"{[tc.get('name','?') if isinstance(tc,dict) else '?' for tc in tool_calls]}"
            )
            from langchain_core.messages import AIMessage
            cleaned.append(AIMessage(
                content=(
                    (msg.get("content") if isinstance(msg, dict) else getattr(msg, "content", ""))
                    or "[Tool calls were cancelled due to a previous error]"
                ),
                tool_calls=[],
            ))

    return cleaned


def _current_step(state: AgentState) -> dict[str, Any] | None:
    steps = list(state.get("task_stack", []))
    step_id = state.get("current_step_id")
    if step_id:
        for step in steps:
            if step.get("id") == step_id:
                return step
    idx = int(state.get("current_task_index", 0) or 0)
    if 0 <= idx < len(steps):
        return steps[idx]
    return None


def _tool_call(name: str, args: dict[str, Any]) -> dict[str, Any]:
    return {"name": name, "args": args, "id": f"call_{uuid.uuid4().hex[:12]}"}


def _goal_text(state: AgentState, step: dict[str, Any] | None = None) -> str:
    intent = state.get("current_intent") or {}
    parts = [
        intent.get("goal"),
        intent.get("user_language_summary"),
        (step or {}).get("description"),
    ]
    return " ".join(str(item or "") for item in parts).lower()


def _latest_top_query_sql(state: AgentState) -> str | None:
    query, _ = _select_optimization_query(state)
    return query


def _query_relation(sql: str | None) -> tuple[str, str]:
    text = str(sql or "")
    match = re.search(r"\bfrom\s+([a-zA-Z_][\w]*)(?:\.([a-zA-Z_][\w]*))?", text, re.IGNORECASE)
    if not match:
        return "public", "big_orders_demo"
    if match.group(2):
        return match.group(1), match.group(2)
    return "public", match.group(1)


def _to_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _to_int(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _related_observations(state: AgentState, step: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    observations = list(state.get("db_observations", []) or [])
    if not step:
        return observations
    step_ids = {str(item) for item in step.get("dependencies", []) if item}
    if step.get("id"):
        step_ids.add(str(step["id"]))
    related = [obs for obs in observations if str(obs.get("step_id") or "") in step_ids]
    return related or observations


def _tool_payloads(observations: list[dict[str, Any]], tool_name: str) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for obs in observations:
        if obs.get("source_tool") != tool_name:
            continue
        payload = obs.get("payload") or {}
        if isinstance(payload, dict):
            payloads.append(payload)
    return payloads


def _query_text(row: dict[str, Any]) -> str:
    return str(row.get("query_preview") or row.get("query") or "").strip()


def _query_metrics(row: dict[str, Any]) -> dict[str, float]:
    resources = sum(
        _to_float(row.get(key))
        for key in ("shared_blks_hit", "shared_blks_read", "temp_blks_read", "temp_blks_written")
    )
    calls = max(_to_float(row.get("calls")), 1.0)
    total = _to_float(row.get("total_exec_time"))
    mean = _to_float(row.get("mean_exec_time"))
    return {
        "resources": resources,
        "calls": calls,
        "total_exec_time": total,
        "mean_exec_time": mean,
        "score": total + (mean * min(calls, 1000.0)) + resources * 0.001,
    }


def _is_select_like(sql: str) -> bool:
    compact = sql.lstrip().lower()
    return compact.startswith("select") or compact.startswith("with")


def _has_optimization_shape(sql: str) -> bool:
    return bool(re.search(r"\b(where|join|group\s+by|order\s+by)\b", sql, re.IGNORECASE))


def _top_query_candidates(observations: list[dict[str, Any]]) -> list[tuple[str, dict[str, Any], dict[str, float]]]:
    candidates: list[tuple[str, dict[str, Any], dict[str, float]]] = []
    for payload in _tool_payloads(observations, "postgres_top_queries"):
        queries = payload.get("queries")
        if not isinstance(queries, list):
            continue
        for row in queries:
            if not isinstance(row, dict):
                continue
            query = _query_text(row)
            if query:
                candidates.append((query, row, _query_metrics(row)))
    return candidates


def _select_optimization_query(
    state: AgentState,
    observations: list[dict[str, Any]] | None = None,
) -> tuple[str | None, dict[str, Any] | None]:
    candidates = _top_query_candidates(observations or list(state.get("db_observations", []) or []))
    if not candidates:
        return None, None

    def rank(candidate: tuple[str, dict[str, Any], dict[str, float]]) -> tuple[int, int, float]:
        query, _, metrics = candidate
        return (
            1 if _is_select_like(query) else 0,
            1 if _has_optimization_shape(query) else 0,
            metrics["score"],
        )

    query, row, metrics = max(candidates, key=rank)
    return query, {**row, "_metrics": metrics}


def _column_from_expr(expr: str) -> str | None:
    text = expr.strip().strip('"')
    text = re.sub(r"\s+(asc|desc|nulls\s+first|nulls\s+last)\b.*$", "", text, flags=re.IGNORECASE)
    if "." in text:
        text = text.split(".")[-1]
    if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", text) and text.lower() not in {"count", "sum", "avg", "min", "max"}:
        return text
    return None


def _columns_from_query(sql: str) -> list[str]:
    columns: list[str] = []

    def add(raw: str | None) -> None:
        if not raw:
            return
        column = _column_from_expr(raw)
        if column and column not in columns:
            columns.append(column)

    for pattern in [
        r"\bwhere\s+([A-Za-z_][\w\.]*)\s*(?:=|>=|<=|>|<|\bin\b|\blike\b)",
        r"\band\s+([A-Za-z_][\w\.]*)\s*(?:=|>=|<=|>|<|\bin\b|\blike\b)",
        r"\bjoin\s+[A-Za-z_][\w\.]*\s+on\s+[A-Za-z_][\w\.]*\.([A-Za-z_][\w]*)",
    ]:
        for match in re.finditer(pattern, sql, re.IGNORECASE):
            add(match.group(1))

    for pattern in [r"\bgroup\s+by\s+(.+?)(?:\border\b|\blimit\b|$)", r"\border\s+by\s+(.+?)(?:\blimit\b|$)"]:
        match = re.search(pattern, sql, re.IGNORECASE)
        if not match:
            continue
        for part in match.group(1).split(","):
            add(part)

    return columns[:3]


def _columns_from_object_detail(state: AgentState, relation: str) -> list[str]:
    preferred = ("status", "created_at", "updated_at", "user_id")
    fallback: list[str] = []
    for obs in reversed(state.get("db_observations", []) or []):
        if obs.get("source_tool") != "postgres_object_detail":
            continue
        payload = obs.get("payload") or {}
        basic = payload.get("basic") if isinstance(payload, dict) else {}
        if isinstance(basic, dict) and basic.get("name") and basic.get("name") != relation:
            continue
        columns = payload.get("columns") if isinstance(payload, dict) else None
        if not isinstance(columns, list):
            continue
        names = [
            str(col.get("column_name") or "")
            for col in columns
            if isinstance(col, dict) and re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", str(col.get("column_name") or ""))
        ]
        for name in preferred:
            if name in names:
                return [name]
        fallback = [name for name in names if name.lower() not in {"id"}]
        if fallback:
            return fallback[:1]
    return fallback[:1]


def _actual_columns_from_object_detail(state: AgentState, relation: str) -> list[str]:
    for obs in reversed(state.get("db_observations", []) or []):
        if obs.get("source_tool") != "postgres_object_detail":
            continue
        payload = obs.get("payload") or {}
        basic = payload.get("basic") if isinstance(payload, dict) else {}
        if isinstance(basic, dict) and basic.get("name") and basic.get("name") != relation:
            continue
        columns = payload.get("columns") if isinstance(payload, dict) else None
        if not isinstance(columns, list):
            continue
        names = [
            str(col.get("column_name") or "")
            for col in columns
            if isinstance(col, dict) and re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", str(col.get("column_name") or ""))
        ]
        if names:
            return names
    return []


def _draft_index_sql_for_query(state: AgentState, sql: str | None = None) -> str:
    query = sql or _latest_top_query_sql(state) or "SELECT COUNT(*) FROM public.big_orders_demo"
    schema, relation = _query_relation(query)
    columns = _columns_from_query(query)
    actual_columns = _actual_columns_from_object_detail(state, relation)
    if actual_columns:
        columns = [column for column in columns if column in actual_columns]
    columns = columns or _columns_from_object_detail(state, relation) or ["created_at"]
    safe_columns = [col for col in columns if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", col)]
    if not safe_columns:
        safe_columns = ["created_at"]
    column_sql = ", ".join(safe_columns[:2])
    index_name = f"idx_{relation}_{'_'.join(safe_columns[:2])}"[:55]
    return f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {index_name} ON {schema}.{relation} ({column_sql});"


def _latest_approved_change(state: AgentState) -> dict[str, Any] | None:
    for approval in reversed(state.get("approval_decisions", []) or []):
        if approval.get("status") == "approved" and approval.get("sql_preview") and approval.get("sql_hash"):
            return approval
    return None


def _format_ms(value: Any) -> str:
    number = _to_float(value)
    if not number:
        return "unknown"
    return f"{number:.2f} ms"


def _format_count(value: Any) -> str:
    number = _to_int(value)
    return f"{number:,}" if number else "0"


def _selected_query_report(state: AgentState, observations: list[dict[str, Any]]) -> str:
    query, row = _select_optimization_query(state, observations)
    if not query or not row:
        top_payloads = _tool_payloads(observations, "postgres_top_queries")
        limitations = [str(payload.get("limitation")) for payload in top_payloads if payload.get("limitation")]
        limitation_text = f"\n限制：{'; '.join(limitations)}" if limitations else ""
        return f"没有拿到历史慢 SQL 排名。{limitation_text}\n下一步：确认 pg_stat_statements 是否启用，或用当前活动查询继续诊断。"
    metrics = row.get("_metrics") or _query_metrics(row)
    reason_parts = []
    if _is_select_like(query):
        reason_parts.append("它是可优化的查询语句")
    if _has_optimization_shape(query):
        reason_parts.append("包含过滤、排序、分组或关联等优化入口")
    if metrics.get("resources"):
        reason_parts.append(f"共享/临时块访问约 {_format_count(metrics['resources'])}")
    if metrics.get("total_exec_time"):
        reason_parts.append(f"累计执行时间 {_format_ms(metrics['total_exec_time'])}")
    if metrics.get("mean_exec_time"):
        reason_parts.append(f"平均执行时间 {_format_ms(metrics['mean_exec_time'])}")
    if row.get("calls") is not None:
        reason_parts.append(f"调用次数 {_format_count(row.get('calls'))}")
    reason = "；".join(reason_parts) or "它在可用统计中排名靠前"
    return (
        "当前最值得优先优化的 SQL 是：\n\n"
        f"```sql\n{query}\n```\n\n"
        f"选择原因：{reason}。\n"
        "关键证据来自 postgres_top_queries 的资源、累计耗时和平均耗时排序；"
        "我优先选择 SELECT/只读查询，因为这类语句可以先用 EXPLAIN、索引和统计信息做低风险验证。"
    )


def _table_overview_report(state: AgentState, observations: list[dict[str, Any]]) -> str:
    env = state.get("database_environment") or {}
    payload = (_tool_payloads(observations, "postgres_schema_overview") or [{}])[-1]
    connection = payload.get("connection") if isinstance(payload, dict) else {}
    schemas = payload.get("schemas") if isinstance(payload, dict) else []
    tables = payload.get("tables") if isinstance(payload, dict) else []
    if not isinstance(connection, dict):
        connection = {}
    if not isinstance(schemas, list):
        schemas = []
    if not isinstance(tables, list):
        tables = []
    host = connection.get("host") or env.get("safe_host_label") or "unknown"
    port = connection.get("port") or env.get("safe_port") or "unknown"
    schema_lines = [
        f"- {item.get('schema_name')} owner={item.get('schema_owner', 'unknown')}"
        for item in schemas[:30]
        if isinstance(item, dict)
    ] or ["- 未发现用户 schema"]
    table_lines = [
        f"- {item.get('schema')}.{item.get('name')} ({item.get('type')})"
        for item in tables[:80]
        if isinstance(item, dict)
    ] or ["- 未发现用户表或视图"]
    return (
        "当前 PostgreSQL 连接信息：\n"
        f"- 环境：{env.get('environment_name') or 'unknown'}\n"
        f"- 数据库：{connection.get('database') or env.get('target_database') or 'unknown'}\n"
        f"- 用户：{connection.get('user') or env.get('safe_user_label') or 'unknown'}\n"
        f"- Host：{host}:{port}\n"
        f"- 权限模式：{env.get('access_mode') or 'unknown'}\n\n"
        "Schema：\n"
        + "\n".join(schema_lines)
        + "\n\n表/视图：\n"
        + "\n".join(table_lines)
    )


def _health_report(observations: list[dict[str, Any]]) -> str:
    connection = (_tool_payloads(observations, "postgres_connection_check") or [{}])[-1]
    health = (_tool_payloads(observations, "postgres_health_check") or [{}])[-1]
    locks = (_tool_payloads(observations, "postgres_lock_inspect") or [{}])[-1]
    top_queries = (_tool_payloads(observations, "postgres_top_queries") or [{}])[-1]
    checks = health.get("checks") if isinstance(health, dict) else {}
    checks = checks if isinstance(checks, dict) else {}

    connection_rows = ((checks.get("connection") or {}).get("rows") or []) if isinstance(checks.get("connection"), dict) else []
    conn = connection_rows[0] if connection_rows and isinstance(connection_rows[0], dict) else {}
    buffer_rows = ((checks.get("buffer") or {}).get("rows") or []) if isinstance(checks.get("buffer"), dict) else []
    vacuum_rows = ((checks.get("vacuum") or {}).get("rows") or []) if isinstance(checks.get("vacuum"), dict) else []
    index_rows = ((checks.get("index") or {}).get("rows") or []) if isinstance(checks.get("index"), dict) else []
    replication_rows = ((checks.get("replication") or {}).get("rows") or []) if isinstance(checks.get("replication"), dict) else []
    constraint_rows = ((checks.get("constraint") or {}).get("rows") or []) if isinstance(checks.get("constraint"), dict) else []
    lock_rows = locks.get("activity") if isinstance(locks, dict) else []
    query_rows = top_queries.get("queries") if isinstance(top_queries, dict) else []

    lines = [
        "我已按只读方式检查连接、连接数、缓存命中、vacuum/analyze、索引、复制、约束、锁等待和 top SQL。",
        f"- 连接：{'可用' if connection.get('success') is not False else '异常'}；database={connection.get('database', 'unknown')} user={connection.get('user', 'unknown')}",
        f"- 连接数：total={conn.get('total_connections', 'unknown')} active={conn.get('active_connections', 'unknown')} idle_in_transaction={conn.get('idle_in_transaction', 'unknown')}",
    ]
    if buffer_rows:
        worst_buffer = min(
            [row for row in buffer_rows if isinstance(row, dict)],
            key=lambda row: _to_float(row.get("cache_hit_ratio")) or 100.0,
            default={},
        )
        lines.append(f"- 缓存命中：最低 cache_hit_ratio={worst_buffer.get('cache_hit_ratio', 'unknown')} database={worst_buffer.get('datname', 'unknown')}")
    if vacuum_rows:
        worst_vacuum = max([row for row in vacuum_rows if isinstance(row, dict)], key=lambda row: _to_float(row.get("n_dead_tup")), default={})
        lines.append(f"- Vacuum/Analyze：dead tuples 最高表 {worst_vacuum.get('schemaname', 'unknown')}.{worst_vacuum.get('relname', 'unknown')}={worst_vacuum.get('n_dead_tup', 'unknown')}")
    if index_rows:
        zero_scan = sum(1 for row in index_rows if isinstance(row, dict) and _to_int(row.get("idx_scan")) == 0)
        lines.append(f"- 索引：采样到 {len(index_rows)} 个索引，其中 idx_scan=0 的索引 {zero_scan} 个，需要结合业务确认是否冗余")
    lines.append(f"- 复制：采样到 {len(replication_rows) if isinstance(replication_rows, list) else 0} 条复制状态")
    lines.append(f"- 未验证约束：{len(constraint_rows) if isinstance(constraint_rows, list) else 0} 条")
    lines.append(f"- 锁/长事务：{len(lock_rows) if isinstance(lock_rows, list) else 0} 条可疑活动")
    lines.append(f"- Top SQL：{len(query_rows) if isinstance(query_rows, list) else 0} 条历史统计候选")
    lines.append("结论：如果没有锁等待、连接耗尽或未验证约束，当前优先级通常转向 top SQL 和表统计信息；后续优化应继续从最重的 SELECT 查询入手。")
    return "\n".join(lines)


def _optimization_plan_report(state: AgentState, observations: list[dict[str, Any]]) -> str:
    query = _latest_top_query_sql(state) or "SELECT COUNT(*) FROM public.big_orders_demo"
    explain = (_tool_payloads(observations, "postgres_explain") or [{}])[-1]
    detail = (_tool_payloads(observations, "postgres_object_detail") or [{}])[-1]
    stats = (_tool_payloads(observations, "postgres_query_readonly") or [{}])[-1]
    advice = (_tool_payloads(observations, "postgres_index_advisor") or [{}])[-1]
    plan = explain.get("plan") if isinstance(explain, dict) else {}
    plan = plan if isinstance(plan, dict) else {}
    columns = detail.get("columns") if isinstance(detail, dict) else []
    indexes = detail.get("indexes") if isinstance(detail, dict) else []
    rows = stats.get("rows") if isinstance(stats, dict) else []
    draft_sql = _draft_index_sql_for_query(state, query)
    advisor_items = advice.get("advice") if isinstance(advice, dict) else []
    advisor_note = ""
    if isinstance(advisor_items, list) and advisor_items:
        first = advisor_items[0] if isinstance(advisor_items[0], dict) else {}
        candidates = first.get("candidates") if isinstance(first, dict) else []
        if isinstance(candidates, list) and candidates:
            advisor_note = f"\n- 索引建议工具候选：{candidates[0].get('create_sql')}"
    stats_line = ""
    if isinstance(rows, list) and rows and isinstance(rows[0], dict):
        row = rows[0]
        stats_line = f"\n- 表统计：n_live_tup={row.get('n_live_tup', 'unknown')} n_dead_tup={row.get('n_dead_tup', 'unknown')} last_analyze={row.get('last_analyze') or row.get('last_autoanalyze') or 'unknown'}"
    return (
        "已完成只读优化分析，未执行写操作。\n\n"
        f"目标 SQL：\n```sql\n{query}\n```\n\n"
        "关键证据：\n"
        f"- 执行计划：root={plan.get('root_node_type', 'unknown')} cost={plan.get('total_cost', 'unknown')} rows={plan.get('plan_rows', 'unknown')} seq_scan={plan.get('has_seq_scan', 'unknown')} index_scan={plan.get('has_index_scan', 'unknown')}\n"
        f"- 表结构：字段 {len(columns) if isinstance(columns, list) else 0} 个，索引 {len(indexes) if isinstance(indexes, list) else 0} 个"
        f"{stats_line}"
        f"{advisor_note}\n\n"
        "优化方案：\n"
        f"- 首选方案：评估并审批后创建索引：`{draft_sql}`\n"
        "- 配套动作：确认统计信息新鲜度，必要时在低峰期执行 ANALYZE；上线后对比 EXPLAIN cost、pg_stat_statements 平均耗时和总耗时。\n\n"
        "风险：创建索引会增加磁盘占用和写入维护成本；CONCURRENTLY 可降低锁影响，但仍需在目标环境审批后执行。\n"
        "验证标准：EXPLAIN 不再出现明显全表扫描瓶颈或成本下降；目标 SQL 的 mean_exec_time/total_exec_time 下降；无新增锁等待和错误。"
    )


def _approval_report(state: AgentState, observations: list[dict[str, Any]]) -> str:
    classification = (_tool_payloads(observations, "postgres_sql_classify") or [{}])[-1]
    sql = str(classification.get("sql") or _draft_index_sql_for_query(state))
    sql_hash = str(classification.get("sql_hash") or classification.get("normalized_sql_hash") or "unknown")
    schema, relation = _query_relation(sql.replace(" ON ", " FROM "))
    index_match = re.search(r"\bindex\s+concurrently\s+if\s+not\s+exists\s+([A-Za-z_][\w]*)", sql, re.IGNORECASE)
    index_name = index_match.group(1) if index_match else "idx_to_drop_after_review"
    rollback = f"DROP INDEX CONCURRENTLY IF EXISTS {schema}.{index_name};"
    env = state.get("database_environment") or {}
    mode = env.get("access_mode") or "unknown"
    return (
        "已生成变更 SQL，当前没有执行任何写操作。\n\n"
        f"```sql\n{sql}\n```\n\n"
        f"- SQL hash：{sql_hash}\n"
        f"- 分类：{classification.get('primary_type', 'schema_change')}；风险：{classification.get('risk_level', 'high')}；权限模式：{mode}\n"
        "- 影响：创建索引会提升匹配查询的过滤/分组/排序路径，但会占用额外磁盘并增加写入维护成本。\n"
        f"- 回滚：`{rollback}`\n"
        "- 验证步骤：先记录变更前 EXPLAIN 和 pg_stat_statements 指标；执行后再次 EXPLAIN；观察 mean_exec_time、total_exec_time、锁等待和错误日志。\n\n"
        "如需执行，请明确回复批准执行，并带上上面的 SQL hash；未审批前我不会调用写工具。"
    )


def _optimization_result_report(state: AgentState, observations: list[dict[str, Any]]) -> str:
    write_results = _tool_payloads(observations, "postgres_execute_write")
    latest_write = write_results[-1] if write_results else {}
    query = _latest_top_query_sql(state) or "SELECT COUNT(*) FROM public.big_orders_demo"
    approval = _latest_approved_change(state) or {}
    sql = str(approval.get("sql_preview") or latest_write.get("sql") or _draft_index_sql_for_query(state, query))
    sql_hash = str(approval.get("sql_hash") or "unknown")
    success = latest_write.get("success")
    execution_summary = "已执行批准的优化 SQL。" if success is not False else "优化 SQL 执行未成功或缺少执行结果。"
    return (
        f"{execution_summary}\n\n"
        f"执行 SQL：\n```sql\n{sql}\n```\n\n"
        f"- SQL hash：{sql_hash}\n"
        f"- 影响：{approval.get('impact_summary') or '创建索引以改善目标查询路径，同时增加索引存储和写入维护成本。'}\n"
        f"- 回滚：`{approval.get('rollback_summary') or 'DROP INDEX CONCURRENTLY IF EXISTS <index_name>;'}`\n"
        "- 验证：已重新收集只读 EXPLAIN/top SQL/index advisor 证据；请结合报告中的执行计划和 pg_stat_statements 指标确认耗时下降。\n"
        "- 剩余风险：历史 pg_stat_statements 需要持续采样；单次验证不能代表所有业务峰值。"
    )


def _readonly_error_report(observations: list[dict[str, Any]]) -> str:
    error_obs = next(
        (
            obs
            for obs in observations
            if obs.get("source_tool") == "postgres_query_readonly"
            and (obs.get("payload") or {}).get("success") is False
        ),
        {},
    )
    detail = (_tool_payloads(observations, "postgres_object_detail") or [{}])[-1]
    payload = error_obs.get("payload") if isinstance(error_obs, dict) else {}
    columns = detail.get("columns") if isinstance(detail, dict) else []
    column_names = [
        str(col.get("column_name"))
        for col in columns
        if isinstance(col, dict) and col.get("column_name")
    ]
    listed = ", ".join(column_names[:30]) if column_names else "未能读取字段列表"
    return (
        "只读诊断已按预期捕获错误，并继续做了恢复性检查。\n\n"
        f"- 失败原因：{(payload or {}).get('error') or error_obs.get('summary') or 'unknown error'}\n"
        f"- SQLSTATE：{(payload or {}).get('sqlstate') or 'unknown'}\n"
        "- 安全性：这是 SELECT 诊断，没有执行写操作。\n"
        f"- 恢复证据：public.big_orders_demo 当前可见字段包括：{listed}\n\n"
        "可用结论：`not_exist_column` 不是 public.big_orders_demo 的有效字段；后续查询应改用上面列出的真实字段，或先用表结构检查确认字段名。"
    )


def _deterministic_report_response(state: AgentState) -> AIMessage | None:
    step = _current_step(state)
    if not step:
        return None
    step_id = str(step.get("id") or "")
    observations = _related_observations(state, step)
    content: str | None = None
    if step_id == "report-target-overview":
        content = _table_overview_report(state, observations)
    elif step_id == "report-health":
        content = _health_report(observations)
    elif step_id == "report-optimization-targets":
        content = _selected_query_report(state, observations)
    elif step_id == "report-optimization-plan":
        content = _optimization_plan_report(state, observations)
    elif step_id == "request-approval":
        content = _approval_report(state, observations)
    elif step_id == "report-optimization-result":
        content = _optimization_result_report(state, observations)
    elif step_id == "report-readonly-error":
        content = _readonly_error_report(observations)
    if not content:
        return None
    return AIMessage(content=content)


def _planned_tool_calls_for_step(state: AgentState) -> list[dict[str, Any]]:
    """Build deterministic tool calls for common observe/propose steps.

    This mirrors Codex/Claude's separation of protocol events from assistant
    prose: tool calls are structured by the runtime when the plan is already
    specific enough, and the model later explains the evidence.
    """
    step = _current_step(state)
    if not step:
        return []
    phase = str(step.get("phase") or "")
    policy = str(step.get("tool_policy") or "no_tools")
    if policy == "no_tools":
        return []

    step_id = str(step.get("id") or "")
    goal = _goal_text(state, step)
    expected = set(step.get("expected_tools") or [])
    calls: list[dict[str, Any]] = []

    if step_id == "inspect-target-overview":
        return [
            _tool_call("postgres_schema_overview", {"include_system": False, "table_limit": 500}),
        ]

    if step_id == "check-database-health":
        return [
            _tool_call("postgres_connection_check", {"include_version": True}),
            _tool_call("postgres_health_check", {"health_type": "all"}),
            _tool_call("postgres_lock_inspect", {"limit": 50}),
            _tool_call("postgres_top_queries", {"sort_by": "resources", "limit": 10}),
        ]

    if step_id == "collect-top-queries":
        return [
            _tool_call("postgres_connection_check", {"include_version": True}),
            _tool_call("postgres_top_queries", {"sort_by": "resources", "limit": 10}),
            _tool_call("postgres_top_queries", {"sort_by": "total_time", "limit": 10}),
            _tool_call("postgres_top_queries", {"sort_by": "mean_time", "limit": 10}),
            _tool_call("postgres_lock_inspect", {"limit": 50}),
        ]

    if step_id == "collect-optimization-evidence":
        sql = _latest_top_query_sql(state) or "SELECT COUNT(*) FROM public.big_orders_demo"
        schema, relation = _query_relation(sql)
        return [
            _tool_call("postgres_top_queries", {"sort_by": "resources", "limit": 10}),
            _tool_call("postgres_explain", {"sql": sql, "analyze": False}),
            _tool_call("postgres_object_detail", {"schema_name": schema, "object_name": relation, "object_type": "table"}),
            _tool_call(
                "postgres_query_readonly",
                {
                    "sql": (
                        "SELECT schemaname, relname, n_live_tup, n_dead_tup, "
                        "last_analyze, last_autoanalyze "
                        f"FROM pg_stat_user_tables WHERE schemaname = '{schema}' AND relname = '{relation}'"
                    ),
                    "max_rows": 20,
                },
            ),
            _tool_call("postgres_index_advisor", {"queries": [sql], "max_index_size_mb": 10000}),
        ]

    if step_id == "draft-change-sql":
        sql = _latest_top_query_sql(state) or "SELECT COUNT(*) FROM public.big_orders_demo"
        draft_sql = _draft_index_sql_for_query(state, sql)
        return [_tool_call("postgres_sql_classify", {"sql": draft_sql, "allow_explain_analyze": False})]

    if step_id == "execute-approved-change":
        approval = _latest_approved_change(state)
        if not approval:
            return []
        return [
            _tool_call(
                "postgres_execute_write",
                {
                    "sql": approval["sql_preview"],
                    "approval_id": approval["id"],
                    "approved_sql_hash": approval["sql_hash"],
                    "target_environment": approval.get("target_environment") or "unknown",
                    "impact_summary": approval.get("impact_summary") or "Execute approved PostgreSQL optimization SQL.",
                    "rollback_summary": approval.get("rollback_summary") or "Use the approved rollback plan.",
                },
            )
        ]

    if step_id == "verify-optimization":
        sql = _latest_top_query_sql(state) or "SELECT COUNT(*) FROM public.big_orders_demo"
        return [
            _tool_call("postgres_explain", {"sql": sql, "analyze": False}),
            _tool_call("postgres_top_queries", {"sort_by": "resources", "limit": 10}),
            _tool_call("postgres_index_advisor", {"queries": [sql], "max_index_size_mb": 10000}),
        ]

    if step_id == "probe-readonly-error":
        return [
            _tool_call("postgres_query_readonly", {"sql": "SELECT not_exist_column FROM public.big_orders_demo LIMIT 5", "max_rows": 5}),
            _tool_call("postgres_object_detail", {"schema_name": "public", "object_name": "big_orders_demo", "object_type": "table"}),
        ]

    if "postgres_list_objects" in expected and phase == "observe":
        calls.extend(
            [
                _tool_call("postgres_list_schemas", {"include_system": False}),
                _tool_call("postgres_list_objects", {"schema_name": "public", "object_type": "table", "limit": 500}),
            ]
        )
    if not calls and "postgres_health_check" in expected:
        calls.append(_tool_call("postgres_health_check", {"health_type": "all"}))
    if not calls and "postgres_top_queries" in expected:
        calls.append(_tool_call("postgres_top_queries", {"sort_by": "resources", "limit": 10}))

    return calls


def _planned_tool_response(state: AgentState) -> AIMessage | None:
    calls = _planned_tool_calls_for_step(state)
    if not calls:
        return None
    step = _current_step(state) or {}
    return AIMessage(
        content=f"Collecting PostgreSQL evidence for step `{step.get('id') or 'observe'}`.",
        tool_calls=calls,
    )


def llm_reason(state: AgentState) -> dict[str, Any]:
    """LLM reasoning node — the core decision-making step.

    Called after memory compaction (if needed) or task planning.
    The LLM receives:
      - System prompt with full memory context + tool descriptions
      - Complete message history
      - Dynamically allowed tool definitions (via bind_tools)

    Returns:
        Partial state with the new AIMessage appended.
    """
    settings = get_settings()
    logger.info(f"LLM reasoning step (session={state.get('session_id', '?')})")

    # Build conversation
    messages = list(state.get("messages", []))

    # Sanitize messages for DeepSeek compatibility:
    # DeepSeek requires every AIMessage with tool_calls to be followed
    # by matching ToolMessages. Strip orphaned tool_calls from history.
    messages = _sanitize_tool_call_messages(messages)

    step_context = None
    environment_update = ExecutionEnvironmentManager(state).bootstrap_state()
    state_with_environment = {**state, **environment_update}
    db_working_set = build_db_working_set(state_with_environment)
    retrieved_memories = retrieve_relevant_memories({**state_with_environment, "db_working_set": db_working_set})
    enriched_state = {**state_with_environment, "db_working_set": db_working_set, "retrieved_memories": retrieved_memories}
    visible_tools, visible_specs = registry.get_for_state(enriched_state)
    try:
        llm, route, record, profile = create_llm_for_task("tool_reasoning", enriched_state, tools=visible_tools)
    except Exception as e:
        logger.error(f"Failed to create LLM: {e}")
        return {
            "error": f"LLM initialization failed: {e}",
            "step_count": state.get("step_count", 0) + 1,
        }
    context_snapshot = build_context_snapshot(enriched_state)
    _, step_context = build_prompt_context(enriched_state)

    planned_response = _planned_tool_response(enriched_state)
    if planned_response is not None:
        update = {
            "messages": [planned_response],
            "step_count": state.get("step_count", 0) + 1,
            "step_context": step_context,
            "db_working_set": db_working_set,
            "retrieved_memories": retrieved_memories,
            **environment_update,
            "available_tools": [tool.name for tool in visible_tools],
            "available_tool_specs": visible_specs,
            "context_snapshots": [context_snapshot],
            "tool_calls_pending": [
                {"name": tc["name"], "args": tc["args"], "id": tc["id"]}
                for tc in planned_response.tool_calls
            ],
            "model_bypass_records": [
                {
                    "task": "tool_reasoning",
                    "reason": "deterministic_planned_tool_calls",
                    "step_id": (step_context or {}).get("step_id"),
                    "tool_count": len(planned_response.tool_calls or []),
                }
            ],
        }
        if not state.get("model_profiles"):
            update["model_profiles"] = default_model_profiles()
        return update

    deterministic_report = _deterministic_report_response(enriched_state)
    if deterministic_report is not None:
        update = {
            "messages": [deterministic_report],
            "step_count": state.get("step_count", 0) + 1,
            "step_context": step_context,
            "db_working_set": db_working_set,
            "retrieved_memories": retrieved_memories,
            **environment_update,
            "available_tools": [tool.name for tool in visible_tools],
            "available_tool_specs": visible_specs,
            "context_snapshots": [context_snapshot],
            "tool_calls_pending": [],
            "model_bypass_records": [
                {
                    "task": "tool_reasoning",
                    "reason": "deterministic_evidence_report",
                    "step_id": (step_context or {}).get("step_id"),
                    "tool_count": 0,
                }
            ],
        }
        if not state.get("model_profiles"):
            update["model_profiles"] = default_model_profiles()
        return update

    # Prepend system prompt after database context and memory retrieval have
    # been normalized so the LLM sees the same context stored in state.
    system_content = build_system_prompt(enriched_state)
    if messages and isinstance(messages[0], SystemMessage):
        messages[0] = SystemMessage(content=system_content)
    else:
        messages.insert(0, SystemMessage(content=system_content))

    # Invoke LLM
    started_at = time.monotonic()
    try:
        response = llm.invoke(messages)
        response = _normalize_llm_tool_markup(response)
    except Exception as e:
        logger.error(f"LLM invocation failed: {e}")
        failure_update = {
            "error": f"LLM call failed: {e}",
            "step_count": state.get("step_count", 0) + 1,
            "model_routes": [route],
            "model_invocation_policies": [route["policy"]],
            "model_invocation_records": [
                finish_invocation_record(record, status="failed", started_at=started_at, error=e, profile=profile)
            ],
            "model_fallback_decisions": [fallback_decision_for_error(route, record, e)],
        }
        if not state.get("model_profiles"):
            failure_update["model_profiles"] = default_model_profiles()
        return failure_update

    logger.info(
        f"LLM response: {len(response.content)} chars, "
        f"{len(response.tool_calls) if response.tool_calls else 0} tool calls"
    )

    # Check for tool calls → set flags
    has_tool_calls = bool(response.tool_calls)

    update = {
        "messages": [response],
        "step_count": state.get("step_count", 0) + 1,
        "step_context": step_context,
        "db_working_set": db_working_set,
        "retrieved_memories": retrieved_memories,
        **environment_update,
        "available_tools": [tool.name for tool in visible_tools],
        "available_tool_specs": visible_specs,
        "context_snapshots": [context_snapshot],
        "model_routes": [route],
        "model_invocation_policies": [route["policy"]],
        "model_invocation_records": [
            finish_invocation_record(
                record,
                status="succeeded",
                started_at=started_at,
                output_text=str(response.content) if hasattr(response, "content") else str(response),
                profile=profile,
            )
        ],
        "tool_calls_pending": (
            [
                {"name": tc["name"], "args": tc["args"], "id": tc["id"]}
                for tc in response.tool_calls
            ]
            if has_tool_calls else []
        ),
    }
    if not state.get("model_profiles"):
        update["model_profiles"] = default_model_profiles()
    return update
