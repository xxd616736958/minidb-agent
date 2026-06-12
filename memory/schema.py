"""Typed schemas and gates for PostgreSQL-safe long-term memory."""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from agent.state import AgentState, MemoryCandidate, MemoryQuery, MemoryRecord


SENSITIVITY_RANK = {"public": 1, "internal": 2, "sensitive": 3, "secret": 4}
SENSITIVE_TEXT_RE = re.compile(
    r"(password|passwd|token|secret|api[_-]?key|connection string|postgresql://|email|phone|ssn|身份证|手机号|邮箱)",
    re.IGNORECASE,
)
WRITE_DENY_RE = re.compile(
    r"(password|passwd|token|secret|api[_-]?key|postgresql://|BEGIN PRIVATE KEY)",
    re.IGNORECASE,
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def expires_at(ttl_seconds: int | None, observed_at: str | None = None) -> str | None:
    if ttl_seconds is None:
        return None
    base = datetime.fromisoformat(observed_at) if observed_at else datetime.now(timezone.utc)
    return (base + timedelta(seconds=ttl_seconds)).isoformat()


def clamp_confidence(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def infer_sensitivity(text: str, default: str = "internal") -> str:
    if WRITE_DENY_RE.search(text):
        return "secret"
    if SENSITIVE_TEXT_RE.search(text):
        return "sensitive"
    return default


def make_memory_record(
    *,
    kind: str,
    scope: str,
    namespace: str,
    summary: str,
    payload: dict[str, Any] | None = None,
    source: str,
    evidence_refs: list[str] | None = None,
    confidence: float = 0.8,
    sensitivity: str | None = None,
    ttl_seconds: int | None = None,
) -> MemoryRecord:
    observed = now_iso()
    text = f"{summary} {payload or {}}"
    sensitivity_value = sensitivity or infer_sensitivity(text)
    return {
        "id": f"mem-{uuid.uuid4().hex[:12]}",
        "kind": kind,  # type: ignore[typeddict-item]
        "scope": scope,  # type: ignore[typeddict-item]
        "namespace": namespace,
        "summary": summary,
        "payload": payload or {},
        "source": source,  # type: ignore[typeddict-item]
        "evidence_refs": evidence_refs or [],
        "confidence": clamp_confidence(confidence),
        "sensitivity": sensitivity_value,  # type: ignore[typeddict-item]
        "ttl_seconds": ttl_seconds,
        "observed_at": observed,
        "expires_at": expires_at(ttl_seconds, observed),
        "supersedes": [],
        "status": "active",
    }


def is_expired(record: MemoryRecord, now: datetime | None = None) -> bool:
    expires = record.get("expires_at")
    if not expires:
        return False
    current = now or datetime.now(timezone.utc)
    return datetime.fromisoformat(expires) <= current


def memory_write_gate(candidate: MemoryCandidate) -> tuple[bool, str]:
    """Return whether a candidate may be written to long-term memory."""
    record = candidate["proposed_record"]
    text = f"{record.get('summary', '')} {record.get('payload', {})}"

    if record.get("sensitivity") == "secret":
        return False, "secret memory is never written"
    if record.get("sensitivity") == "sensitive":
        return False, "sensitive memory is not written by default"
    if WRITE_DENY_RE.search(text):
        return False, "memory contains credential-like content"
    if record.get("confidence", 0) < 0.5:
        return False, "confidence too low"
    if record.get("kind") == "assumption" and candidate.get("write_decision") != "approved":
        return False, "assumptions require explicit approval"
    if candidate.get("requires_user_confirmation") and candidate.get("write_decision") != "approved":
        return False, "user confirmation required"
    return True, "allowed"


def memory_read_gate(record: MemoryRecord, query: MemoryQuery) -> tuple[bool, str]:
    """Return whether a memory record can enter current context."""
    if record.get("status") != "active":
        return False, f"record status is {record.get('status')}"
    if is_expired(record):
        return False, "record expired"
    if record.get("scope") not in set(query.get("allowed_scopes", [])):
        return False, "scope not allowed"
    max_sensitivity = query.get("max_sensitivity", "internal")
    if SENSITIVITY_RANK[record.get("sensitivity", "internal")] > SENSITIVITY_RANK[max_sensitivity]:
        return False, "sensitivity too high"
    if record.get("confidence", 0) < 0.4:
        return False, "confidence too low"

    target_env = query.get("target_environment")
    payload_env = record.get("payload", {}).get("target_environment")
    if payload_env and target_env and payload_env != target_env:
        return False, "target environment mismatch"
    return True, "allowed"


def build_memory_query(state: AgentState) -> MemoryQuery:
    intent = state.get("current_intent") or {}
    step_context = state.get("step_context") or {}
    target_objects = []
    for obj in intent.get("target_objects", []) or []:
        if isinstance(obj, dict) and obj.get("name"):
            target_objects.append(str(obj["name"]))
    return {
        "intent_type": str(intent.get("primary_intent") or ""),
        "step_phase": str(step_context.get("phase") or ""),
        "target_environment": str(intent.get("target_environment") or "unknown"),
        "target_database": intent.get("target_database"),
        "target_objects": target_objects,
        "risk_level": str(intent.get("risk_level") or "low"),
        "allowed_scopes": ["user", "project", "database", "schema", "session", "task"],
        "max_sensitivity": "internal",
    }


def candidate_from_user_preference(summary: str, namespace: str = "user") -> MemoryCandidate:
    record = make_memory_record(
        kind="preference",
        scope="user",
        namespace=namespace,
        summary=summary,
        source="user_confirmed",
        confidence=0.9,
        sensitivity="internal",
    )
    return {
        "id": f"cand-{uuid.uuid4().hex[:12]}",
        "proposed_record": record,
        "reason": "user preference can improve future responses",
        "requires_user_confirmation": False,
        "write_decision": "auto_write",
    }
