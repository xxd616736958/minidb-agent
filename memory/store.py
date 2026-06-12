"""Long-term MemoryRecord store abstraction.

The in-memory implementation is intentionally small and deterministic for local
tests. It can be replaced by LangGraph Store or PostgreSQL-backed persistence
without changing the gates and callers.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from agent.config import get_settings
from agent.state import MemoryQuery, MemoryRecord
from memory.schema import is_expired, memory_read_gate


@dataclass
class MemoryStore:
    records: dict[str, MemoryRecord] = field(default_factory=dict)
    path: str | None = None
    autoload: bool = True

    def __post_init__(self) -> None:
        if self.path and self.autoload:
            self.load()

    def upsert(self, record: MemoryRecord) -> MemoryRecord:
        """Insert or update a record, superseding older records in the same lane."""
        record = dict(record)
        superseded_ids: list[str] = []
        conflict_key = self._conflict_key(record)

        if conflict_key and record.get("status") == "active":
            for existing_id, existing in list(self.records.items()):
                if existing_id == record["id"]:
                    continue
                if existing.get("status") != "active":
                    continue
                if self._conflict_key(existing) != conflict_key:
                    continue

                updated = dict(existing)
                updated["status"] = "deprecated"
                updated["payload"] = {
                    **updated.get("payload", {}),
                    "deprecation_reason": f"superseded by {record['id']}",
                }
                self.records[existing_id] = updated
                superseded_ids.append(existing_id)

        if superseded_ids:
            record["supersedes"] = sorted(set(record.get("supersedes", []) + superseded_ids))

        self.records[record["id"]] = record
        self.save()
        return record

    def get(self, record_id: str) -> MemoryRecord | None:
        return self.records.get(record_id)

    def deprecate(self, record_id: str, reason: str = "") -> MemoryRecord | None:
        record = self.records.get(record_id)
        if not record:
            return None
        record = dict(record)
        record["status"] = "deprecated"
        if reason:
            record["payload"] = {**record.get("payload", {}), "deprecation_reason": reason}
        self.records[record_id] = record
        self.save()
        return record

    def mark_conflicted(self, record_id: str, reason: str) -> MemoryRecord | None:
        record = self.records.get(record_id)
        if not record:
            return None
        record = dict(record)
        record["status"] = "conflicted"
        record["payload"] = {**record.get("payload", {}), "conflict_reason": reason}
        self.records[record_id] = record
        self.save()
        return record

    def expire_stale(self) -> list[MemoryRecord]:
        expired: list[MemoryRecord] = []
        for record_id, record in list(self.records.items()):
            if record.get("status") != "active" or not is_expired(record):
                continue
            updated = dict(record)
            updated["status"] = "expired"
            self.records[record_id] = updated
            expired.append(updated)
        if expired:
            self.save()
        return expired

    def search(self, query: MemoryQuery, limit: int = 5) -> list[MemoryRecord]:
        candidates = []
        terms = {
            query.get("intent_type", "").lower(),
            query.get("step_phase", "").lower(),
            query.get("target_environment", "").lower(),
            str(query.get("target_database") or "").lower(),
            *(item.lower() for item in query.get("target_objects", [])),
        }
        terms = {term for term in terms if term}

        self.expire_stale()
        for record in self.records.values():
            allowed, _ = memory_read_gate(record, query)
            if not allowed:
                continue
            score = self._score(record, terms, query)
            if score <= 0 and record.get("kind") not in {"preference", "prohibition"}:
                continue
            candidates.append((score, record))

        candidates.sort(key=lambda item: (item[0], item[1].get("confidence", 0)), reverse=True)
        return [record for _, record in candidates[:limit]]

    @staticmethod
    def _conflict_key(record: MemoryRecord) -> tuple | None:
        payload = record.get("payload", {})
        explicit_key = payload.get("memory_key")
        if explicit_key:
            return (record.get("kind"), record.get("scope"), record.get("namespace"), explicit_key)

        kind = record.get("kind")
        if kind == "schema_summary":
            return (
                kind,
                record.get("scope"),
                record.get("namespace"),
                payload.get("observation_type"),
                payload.get("target_database"),
            )
        if kind in {"preference", "prohibition"}:
            return (
                kind,
                record.get("scope"),
                record.get("namespace"),
                record.get("summary", "").strip().lower(),
            )
        return None

    @staticmethod
    def _score(record: MemoryRecord, terms: set[str], query: MemoryQuery) -> int:
        payload = record.get("payload", {})
        haystack = (
            f"{record.get('kind')} {record.get('scope')} {record.get('namespace')} "
            f"{record.get('summary')} {payload}"
        ).lower()
        score = sum(2 for term in terms if term in haystack)

        kind = record.get("kind")
        if kind == "prohibition":
            score += 100
        elif kind == "preference":
            score += 40
        elif kind in {"schema_summary", "experience"}:
            score += 10

        if payload.get("target_environment") == query.get("target_environment"):
            score += 8
        if payload.get("target_database") and payload.get("target_database") == query.get("target_database"):
            score += 8
        return score

    def load(self) -> None:
        if not self.path:
            return
        path = Path(self.path)
        if not path.exists():
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        records = payload.get("records", payload)
        if not isinstance(records, list):
            return
        self.records = {
            item["id"]: item
            for item in records
            if isinstance(item, dict) and item.get("id")
        }

    def save(self) -> None:
        if not self.path:
            return
        path = Path(self.path)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_suffix(path.suffix + ".tmp")
        payload = {
            "records": list(self.records.values()),
        }
        temp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(temp_path, path)


GLOBAL_MEMORY_STORE = MemoryStore(path=get_settings().memory_store_path)


def get_memory_store() -> MemoryStore:
    return GLOBAL_MEMORY_STORE
