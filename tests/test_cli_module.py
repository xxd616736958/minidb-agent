"""Tests for the PostgreSQL-focused CLI control plane."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cli.config import (
    CliRuntimeConfig,
    build_agent_input_context,
    build_db_connection_card,
    mask_database_url,
    runtime_config_from_args,
)
from cli.doctor import doctor_exit_code, run_doctor
from cli.events import CliEventAdapter, sanitize_for_cli
from cli.exec_mode import run_exec
from cli.main import parse_args
from cli.repl import AgentRepl
from cli.sessions import SessionIndex, record_from_runtime


def test_runtime_config_and_connection_card_are_database_focused(tmp_path):
    args = parse_args(
        [
            "--database-url",
            "postgresql://dbuser:secret@db.example.com:5432/appdb",
            "--target-env",
            "prod",
            "--readonly",
            "--approval-mode",
            "always",
            "--workspace",
            str(tmp_path),
            "exec",
            "--jsonl",
            "check database",
        ]
    )

    config = runtime_config_from_args(args)
    card = build_db_connection_card(config)

    assert config.output_mode == "jsonl"
    assert config.target_environment == "prod"
    assert config.readonly is True
    assert card["host"] == "db.example.com"
    assert card["database"] == "appdb"
    assert card["user"] == "dbuser"
    assert "secret" not in card["display_url"]
    assert card["display_url"] == "postgresql://dbuser:***@db.example.com:5432/appdb"


def test_agent_input_context_does_not_persist_database_url(tmp_path):
    config = CliRuntimeConfig(
        database_url="postgresql://dbuser:secret@localhost:5432/appdb",
        target_environment="prod",
        readonly=True,
        workspace=str(tmp_path),
        approval_mode="never",
    )

    context = build_agent_input_context(config)
    serialized = json.dumps(context, default=str)

    assert "secret" not in serialized
    assert "postgresql://dbuser" not in serialized
    assert context["database_environment"]["environment_name"] == "production"
    assert context["database_environment"]["allow_write_tools"] is False
    assert context["runtime_policy"]["allow_database_writes"] is False


def test_mask_database_url_handles_url_and_dsn():
    assert mask_database_url("postgresql://u:p@host:5432/db") == "postgresql://u:***@host:5432/db"
    assert mask_database_url("host=localhost password=secret user=db") == "host=localhost password=*** user=db"


def test_cli_event_adapter_maps_nodes_to_stable_database_events():
    adapter = CliEventAdapter("thread-1")
    events = adapter.events_from_stream_data(
        {
            "intent_validator": {"current_intent": {"user_language_summary": "检查慢 SQL", "risk_level": "low"}},
            "tool_policy_gate": {
                "approval_card": {"risk_level": "high", "sql_hash": "abc123"},
                "pending_approval": {"id": "approval-1"},
            },
            "final_report": {
                "delivery_packages": [
                    {"status": "ready", "title": "慢 SQL 诊断报告", "user_report_path": "/tmp/report.md"}
                ]
            },
        }
    )

    assert [event["type"] for event in events] == [
        "task_understanding",
        "safety_check",
        "approval_required",
        "delivery_ready",
    ]
    assert events[0]["thread_id"] == "thread-1"
    assert "abc123" in events[2]["summary"]


def test_sanitize_for_cli_masks_secret_keys_and_postgres_urls():
    payload = {
        "api_key": "sk-secret",
        "text": "connect postgresql://u:p@host/db now",
        "nested": {"password": "secret"},
    }

    sanitized = sanitize_for_cli(payload)
    serialized = json.dumps(sanitized)

    assert "sk-secret" not in serialized
    assert '"api_key": "***"' in serialized
    assert "p@host" not in serialized
    assert "postgresql://u:***@host/db" in serialized
    assert sanitized["nested"]["password"] == "***"


def test_session_index_upsert_list_archive_and_latest(tmp_path):
    index = SessionIndex(tmp_path / "sessions.json")
    config = CliRuntimeConfig(
        database_url="postgresql://u:p@localhost:5432/appdb",
        workspace=str(tmp_path),
        target_environment="dev",
    )
    record = record_from_runtime(
        config,
        thread_id="thread-1",
        state_values={
            "current_intent": {"primary_intent": "performance", "user_language_summary": "检查慢 SQL"},
            "db_task_runtime": {"task_status": "completed"},
        },
    )

    saved = index.upsert(record)
    assert saved["title"] == "检查慢 SQL"
    assert index.list()[0]["thread_id"] == "thread-1"
    assert index.latest_for_config(config)["thread_id"] == "thread-1"
    assert index.archive("thread-1") is True
    assert index.list() == []
    assert index.list(include_archived=True)[0]["archived"] is True


@pytest.mark.asyncio
async def test_doctor_without_database_url_reports_warning(tmp_path):
    config = CliRuntimeConfig(workspace=str(tmp_path), target_environment="dev")
    report = await run_doctor(config, check_server=False, check_database=True)

    assert report["status"] == "warning"
    assert doctor_exit_code(report) == 0
    checks = {item["name"]: item for item in report["checks"]}
    assert checks["database_config"]["status"] == "warning"
    assert checks["workspace"]["status"] == "passed"
    assert checks["artifact_workspace"]["status"] == "passed"


class _FakeEvent:
    def __init__(self, data):
        self.data = data


class _FakeRuns:
    def __init__(self):
        self.cancelled = []

    async def stream(self, **kwargs):
        yield _FakeEvent({"intent_validator": {"current_intent": {"user_language_summary": "检查索引"}}})
        yield _FakeEvent(
            {
                "final_report": {
                    "delivery_packages": [
                        {"status": "ready", "title": "索引建议", "user_report_path": "/tmp/report.md"}
                    ]
                }
            }
        )

    async def list(self, thread_id, limit=10):
        return [{"run_id": "run-1", "status": "running"}]

    async def cancel(self, thread_id, run_id, wait=False, action="interrupt"):
        self.cancelled.append({"thread_id": thread_id, "run_id": run_id, "wait": wait, "action": action})


class _FakeThreads:
    async def create(self):
        return {"thread_id": "thread-exec"}

    async def get_state(self, thread_id):
        return {
            "values": {
                "db_task_runtime": {"task_status": "completed"},
                "current_intent": {"primary_intent": "index_advice", "user_language_summary": "检查索引"},
                "delivery_packages": [
                    {"status": "ready", "title": "索引建议", "user_report_path": "/tmp/report.md"}
                ],
            }
        }


class _FakeAssistants:
    async def search(self):
        return [{"assistant_id": "agent"}]


class _FakeClient:
    def __init__(self):
        self.runs = _FakeRuns()
        self.threads = _FakeThreads()
        self.assistants = _FakeAssistants()


@pytest.mark.asyncio
async def test_exec_mode_jsonl_writes_stable_events(tmp_path, monkeypatch):
    output_file = tmp_path / "events.jsonl"
    monkeypatch.setenv("MINIDB_AGENT_HOME", str(tmp_path / "home"))
    config = CliRuntimeConfig(
        server_url="http://server",
        workspace=str(tmp_path),
        output_mode="jsonl",
        output_file=str(output_file),
        save_session=True,
    )

    code = await run_exec(config, "检查索引", client_factory=lambda **kwargs: _FakeClient())

    assert code == 0
    lines = [json.loads(line) for line in output_file.read_text(encoding="utf-8").splitlines()]
    assert [line["type"] for line in lines] == ["task_understanding", "delivery_ready"]
    assert (Path(tmp_path) / "home" / "sessions.json").exists()


@pytest.mark.asyncio
async def test_exec_mode_json_writes_final_result(tmp_path):
    output_file = tmp_path / "result.json"
    config = CliRuntimeConfig(
        server_url="http://server",
        workspace=str(tmp_path),
        output_mode="json",
        output_file=str(output_file),
        save_session=False,
    )

    code = await run_exec(config, "检查索引", client_factory=lambda **kwargs: _FakeClient())

    assert code == 0
    payload = json.loads(output_file.read_text(encoding="utf-8"))
    assert payload["thread_id"] == "thread-exec"
    assert payload["status"] == "completed"
    assert payload["delivery_packages"][0]["title"] == "索引建议"


@pytest.mark.asyncio
async def test_repl_cancel_calls_running_run_cancel(tmp_path):
    repl = AgentRepl(
        runtime_config=CliRuntimeConfig(server_url="http://server", workspace=str(tmp_path)),
        thread_id="thread-1",
    )
    fake_client = _FakeClient()
    repl.client = fake_client

    await repl._cmd_cancel()

    assert fake_client.runs.cancelled == [
        {"thread_id": "thread-1", "run_id": "run-1", "wait": False, "action": "interrupt"}
    ]
