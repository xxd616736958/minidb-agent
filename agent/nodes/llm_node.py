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
