"""Local CLI session index keyed by project and database fingerprint."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from cli.config import CliRuntimeConfig, build_db_connection_card, session_index_path


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class SessionIndex:
    """Small JSON-backed session index for CLI resume workflows."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path).expanduser() if path else session_index_path()

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"sessions": []}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {"sessions": []}
        sessions = data.get("sessions") if isinstance(data, dict) else None
        return {"sessions": sessions if isinstance(sessions, list) else []}

    def save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        tmp.replace(self.path)

    def list(
        self,
        *,
        include_archived: bool = False,
        project_dir: str | None = None,
        database_fingerprint: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        records = self.load()["sessions"]
        filtered = []
        for record in records:
            if not include_archived and record.get("archived"):
                continue
            if not has_resume_content(record):
                continue
            if project_dir and record.get("project_dir") != project_dir:
                continue
            if database_fingerprint and record.get("database_fingerprint") != database_fingerprint:
                continue
            filtered.append(record)
        filtered.sort(key=lambda item: item.get("updated_at") or "", reverse=True)
        return filtered[:limit]

    def upsert(self, record: dict[str, Any]) -> dict[str, Any]:
        data = self.load()
        records = data["sessions"]
        now = now_iso()
        record = dict(record)
        record.setdefault("created_at", now)
        record["updated_at"] = now
        record.setdefault("archived", False)
        thread_id = record.get("thread_id")
        replaced = False
        for idx, existing in enumerate(records):
            if existing.get("thread_id") == thread_id:
                merged = {**existing, **record, "created_at": existing.get("created_at") or record["created_at"]}
                records[idx] = merged
                record = merged
                replaced = True
                break
        if not replaced:
            records.append(record)
        self.save({"sessions": records})
        return record

    def archive(self, thread_id: str, archived: bool = True) -> bool:
        data = self.load()
        changed = False
        for record in data["sessions"]:
            if record.get("thread_id") == thread_id:
                record["archived"] = archived
                record["updated_at"] = now_iso()
                changed = True
                break
        if changed:
            self.save(data)
        return changed

    def latest_for_config(self, config: CliRuntimeConfig) -> Optional[dict[str, Any]]:
        card = build_db_connection_card(config)
        project_dir = str(Path(config.workspace).expanduser().resolve())
        records = self.list(project_dir=project_dir, database_fingerprint=card["fingerprint"], limit=1)
        if records:
            return records[0]
        records = self.list(project_dir=project_dir, limit=1)
        if records:
            return records[0]
        records = self.list(limit=1)
        return records[0] if records else None


def has_resume_content(record: dict[str, Any]) -> bool:
    """Return whether a record represents a real resumable conversation."""
    title = str(record.get("title") or "").strip()
    thread_id = str(record.get("thread_id") or "")
    fallback = f"Session {thread_id[:8]}"
    if record.get("last_intent"):
        return True
    if record.get("artifact_paths"):
        return True
    if str(record.get("last_status") or "unknown") != "unknown":
        return True
    return bool(title and title != fallback)


def record_from_runtime(
    config: CliRuntimeConfig,
    *,
    thread_id: str,
    title: str | None = None,
    state_values: dict[str, Any] | None = None,
    server_info: dict[str, Any] | None = None,
) -> dict[str, Any]:
    state_values = state_values or {}
    card = build_db_connection_card(config, server_info)
    intent = state_values.get("current_intent") or {}
    packages = state_values.get("delivery_packages") or []
    artifact_paths = []
    if packages:
        latest = packages[-1]
        artifact_paths.extend(
            path
            for path in [
                latest.get("user_report_path"),
                latest.get("audit_report_path"),
                latest.get("manifest_path"),
            ]
            if path
        )
    return {
        "thread_id": thread_id,
        "title": title or intent.get("user_language_summary") or intent.get("goal") or f"Session {thread_id[:8]}",
        "project_dir": str(Path(config.workspace).expanduser().resolve()),
        "database_fingerprint": card["fingerprint"],
        "target_environment": card["target_environment"],
        "last_intent": intent.get("primary_intent"),
        "last_status": _last_status(state_values),
        "artifact_paths": artifact_paths,
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "archived": False,
    }


def _last_status(state_values: dict[str, Any]) -> str:
    runtime = state_values.get("db_task_runtime") or {}
    if runtime.get("task_status"):
        return str(runtime["task_status"])
    packages = state_values.get("delivery_packages") or []
    if packages:
        return str(packages[-1].get("status") or "delivered")
    return "unknown"
