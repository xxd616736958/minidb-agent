"""Memory consolidation for verified PostgreSQL task state."""

from __future__ import annotations

import uuid
import re
from typing import Any

from agent.state import AgentState, MemoryCandidate, MemoryRecord
from memory.schema import make_memory_record, memory_write_gate
from memory.store import get_memory_store


EXPLICIT_REMEMBER_RE = re.compile(
    r"(记住|请记住|以后.*(都|默认|使用|不要|只读|禁止)|下次.*(都|默认|使用|不要|只读|禁止)|remember|always|prefer|preference|default)",
    re.IGNORECASE,
)
SAFETY_MEMORY_RE = re.compile(
    r"(只读|read[- ]only|不要执行|禁止|不得|不能|never|must not|do not execute|no\s+(drop|truncate|delete|update))",
    re.IGNORECASE,
)


def _namespace(state: AgentState) -> str:
    intent = state.get("current_intent") or {}
    database = intent.get("target_database") or "unknown-db"
    environment = intent.get("target_environment") or "unknown-env"
    return f"{environment}:{database}"


def _candidate(record: MemoryRecord, reason: str, requires_user_confirmation: bool = False) -> MemoryCandidate:
    return {
        "id": f"cand-{uuid.uuid4().hex[:12]}",
        "proposed_record": record,
        "reason": reason,
        "requires_user_confirmation": requires_user_confirmation,
        "write_decision": "pending" if requires_user_confirmation else "auto_write",
    }


def _clean_explicit_memory_text(text: str) -> str:
    cleaned = re.sub(r"^\s*(请)?记住[:：,，\s]*", "", text, flags=re.IGNORECASE)
    cleaned = re.sub(r"^\s*remember\s+(that\s+)?", "", cleaned, flags=re.IGNORECASE)
    return cleaned.strip() or text.strip()


def generate_explicit_memory_candidates(text: str, state: AgentState) -> list[MemoryCandidate]:
    """Generate candidates only when the user explicitly asks the agent to remember."""
    if not text or not EXPLICIT_REMEMBER_RE.search(text):
        return []

    summary = _clean_explicit_memory_text(text)
    is_safety = bool(SAFETY_MEMORY_RE.search(summary))
    record = make_memory_record(
        kind="prohibition" if is_safety else "preference",
        scope="user",
        namespace="safety" if is_safety else "user",
        summary=summary,
        payload={
            "memory_key": f"explicit:{'safety' if is_safety else 'preference'}:{summary[:80]}",
        },
        source="user_confirmed",
        evidence_refs=[],
        confidence=0.95,
    )
    return [
        _candidate(
            record,
            "user explicitly asked the agent to remember this preference or safety rule",
        )
    ]


def consolidate_explicit_memory_request(state: AgentState, text: str) -> dict[str, Any]:
    """Persist explicit user memory requests after the normal write gate."""
    candidates = generate_explicit_memory_candidates(text, state)
    if not candidates:
        return {}

    written: list[MemoryRecord] = []
    safe_candidates: list[MemoryCandidate] = []
    store = get_memory_store()
    for candidate in candidates:
        allowed, reason = memory_write_gate(candidate)
        if not allowed:
            if "credential" in reason or "secret" in reason:
                continue
            safe_candidates.append(candidate)
            continue
        safe_candidates.append(candidate)
        written.append(store.upsert(candidate["proposed_record"]))

    if not safe_candidates and not written:
        return {}
    return {
        "memory_candidates": safe_candidates,
        "memory_records_written": written,
    }


def generate_memory_candidates(state: AgentState) -> list[MemoryCandidate]:
    """Generate safe memory candidates from verified state."""
    candidates: list[MemoryCandidate] = []
    namespace = _namespace(state)
    intent = state.get("current_intent") or {}
    verification_results = state.get("verification_results", [])
    observations = state.get("db_observations", [])
    constraints = list(intent.get("constraints", []) or []) + list(state.get("user_constraints", []) or [])

    for constraint in constraints:
        if not constraint:
            continue
        if "只读" in constraint or "read-only" in constraint.lower() or "不要执行" in constraint:
            record = make_memory_record(
                kind="prohibition",
                scope="user",
                namespace="safety",
                summary=f"User safety preference: {constraint}",
                payload={"constraint": constraint},
                source="user_confirmed",
                evidence_refs=[],
                confidence=0.95,
                sensitivity="internal",
            )
            candidates.append(_candidate(record, "user safety constraint should persist"))

    passed_steps = {
        item.get("step_id")
        for item in verification_results
        if item.get("status") == "passed"
    }
    for obs in observations:
        if obs.get("step_id") not in passed_steps:
            continue
        obs_type = obs.get("type")
        if obs_type in {"schema_summary", "index_summary", "row_count_estimate"}:
            record = make_memory_record(
                kind="schema_summary",
                scope="database",
                namespace=namespace,
                summary=f"{obs_type}: {obs.get('summary', '')[:240]}",
                payload={
                    "observation_type": obs_type,
                    "target_environment": intent.get("target_environment", "unknown"),
                    "target_database": intent.get("target_database"),
                },
                source="tool_observed",
                evidence_refs=[obs["id"]],
                confidence=0.8,
                sensitivity="internal",
                ttl_seconds=7 * 24 * 3600,
            )
            candidates.append(_candidate(record, "verified database metadata can help future diagnostics"))
        elif obs_type == "explain_plan":
            record = make_memory_record(
                kind="experience",
                scope="database",
                namespace=namespace,
                summary=f"Observed execution plan evidence: {obs.get('summary', '')[:240]}",
                payload={
                    "observation_type": obs_type,
                    "target_environment": intent.get("target_environment", "unknown"),
                    "target_database": intent.get("target_database"),
                },
                source="tool_observed",
                evidence_refs=[obs["id"]],
                confidence=0.75,
                sensitivity="internal",
                ttl_seconds=14 * 24 * 3600,
            )
            candidates.append(_candidate(record, "verified performance evidence can help future diagnostics"))

    approval_decisions = state.get("approval_decisions", [])
    for decision in approval_decisions:
        if decision.get("status") not in {"rejected", "edited"}:
            continue
        record = make_memory_record(
            kind="experience",
            scope="user",
            namespace="safety",
            summary=(
                "User approval preference: "
                f"{decision.get('status')} SQL for risk={decision.get('risk_level')} "
                f"env={decision.get('target_environment')}"
            ),
            payload={
                "approval_status": decision.get("status"),
                "risk_level": decision.get("risk_level"),
                "target_environment": decision.get("target_environment"),
                "step_id": decision.get("step_id"),
                "memory_key": f"approval:{decision.get('step_id')}:{decision.get('status')}",
            },
            source="user_confirmed",
            evidence_refs=[decision["id"]] if decision.get("id") else [],
            confidence=0.8,
            sensitivity="internal",
            ttl_seconds=90 * 24 * 3600,
        )
        candidates.append(_candidate(record, "approval history can inform future risk prompts"))

    steps = list((state.get("db_task_plan") or {}).get("steps", []) or state.get("task_stack", []))
    task_completed = bool(steps) and all(step.get("status") in {"completed", "skipped"} for step in steps)
    report_completed = any(
        step.get("phase") == "report"
        and step.get("status") == "completed"
        and step.get("id") in passed_steps
        for step in steps
    )
    if intent.get("domain") == "postgresql" and verification_results and (task_completed or report_completed):
        passed = [item for item in verification_results if item.get("status") == "passed"]
        if passed:
            record = make_memory_record(
                kind="task_episode",
                scope="database",
                namespace=namespace,
                summary=f"Completed PostgreSQL task: {intent.get('goal', 'unknown goal')}",
                payload={
                    "intent_type": intent.get("primary_intent"),
                    "target_environment": intent.get("target_environment"),
                    "target_database": intent.get("target_database"),
                    "passed_steps": [item.get("step_id") for item in passed],
                },
                source="report_generated",
                evidence_refs=[item["id"] for item in passed if item.get("id")],
                confidence=0.7,
                sensitivity="internal",
                ttl_seconds=30 * 24 * 3600,
            )
            candidates.append(_candidate(record, "verified task episode can support future reports"))

    return candidates


def consolidate_memories(state: AgentState) -> dict[str, Any]:
    """Generate candidates and write those allowed by memory_write_gate."""
    candidates = generate_memory_candidates(state)
    written: list[MemoryRecord] = []
    store = get_memory_store()

    for candidate in candidates:
        allowed, _ = memory_write_gate(candidate)
        if not allowed:
            continue
        record = store.upsert(candidate["proposed_record"])
        written.append(record)

    if not candidates and not written:
        return {}
    return {
        "memory_candidates": candidates,
        "memory_records_written": written,
    }
