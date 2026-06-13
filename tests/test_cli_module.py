"""Tests for the PostgreSQL-focused CLI control plane."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cli.config import (
    CliRuntimeConfig,
    build_agent_input_context,
    build_db_connection_card,
    load_user_config,
    mask_database_url,
    persist_runtime_defaults,
    runtime_config_from_args,
)
from cli.doctor import doctor_exit_code, run_doctor
from cli.events import CliEventAdapter, sanitize_for_cli
from cli.exec_mode import _exit_code_from_state, run_exec
from cli.main import _pick_thread_id, _select_thread_id, parse_args
from cli.repl import AgentRepl, SlashCommandCompleter
from cli.setup_flow import ensure_database_config
from cli.session_picker import session_label
from cli.sessions import SessionIndex, has_resume_content, record_from_runtime
from agent.nodes.approval_response import apply_cli_approval_response
from tools.postgres.results import dumps_result, make_result


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


def test_runtime_config_reads_postgres_target_env(monkeypatch, tmp_path):
    monkeypatch.setenv("POSTGRES_TARGET_URL", "postgresql://db_agent:db_agent_dev@127.0.0.1:5432/db_agent")
    monkeypatch.setenv("POSTGRES_TARGET_ENV", "dev")
    args = parse_args(["--workspace", str(tmp_path), "sessions"])

    config = runtime_config_from_args(args)
    card = build_db_connection_card(config)

    assert config.database_url == "postgresql://db_agent:db_agent_dev@127.0.0.1:5432/db_agent"
    assert config.target_environment == "dev"
    assert card["host"] == "127.0.0.1"
    assert card["database"] == "db_agent"
    assert card["display_url"] == "postgresql://db_agent:***@127.0.0.1:5432/db_agent"


def test_runtime_config_reads_persisted_database_defaults(monkeypatch, tmp_path):
    monkeypatch.setenv("MINIDB_AGENT_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("POSTGRES_TARGET_URL", raising=False)
    monkeypatch.delenv("POSTGRES_TARGET_ENV", raising=False)
    persist_runtime_defaults(
        CliRuntimeConfig(
            database_url="postgresql://saved:secret@db.local:5433/app",
            target_environment="staging",
            approval_mode="always",
            server_url="http://127.0.0.1:2025",
        )
    )

    config = runtime_config_from_args(parse_args(["--workspace", str(tmp_path)]))

    assert config.database_url == "postgresql://saved:secret@db.local:5433/app"
    assert config.target_environment == "staging"
    assert config.approval_mode == "always"
    assert config.server_url == "http://127.0.0.1:2025"


def test_ensure_database_config_persists_existing_cli_config(monkeypatch, tmp_path):
    monkeypatch.setenv("MINIDB_AGENT_HOME", str(tmp_path / "home"))
    config = CliRuntimeConfig(
        database_url="postgresql://u:p@localhost:5432/appdb",
        target_environment="dev",
        workspace=str(tmp_path),
    )

    resolved = ensure_database_config(config, interactive=False)

    assert resolved == config
    stored = load_user_config()
    assert stored["database_url"] == "postgresql://u:p@localhost:5432/appdb"
    assert stored["target_environment"] == "dev"


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


def test_agent_input_context_uses_server_database_environment_without_secret(tmp_path):
    config = CliRuntimeConfig(workspace=str(tmp_path), target_environment="unknown")
    server_info = {
        "postgres": {"target_configured": True, "credential_ref": "env:POSTGRES_TARGET_URL"},
        "database_environment": {
            "environment_name": "dev",
            "target_database": "db_agent",
            "safe_host_label": "127.0.0.1",
            "safe_user_label": "db_agent",
            "default_statement_timeout_ms": 1000,
            "default_lock_timeout_ms": 500,
            "max_result_rows": 25,
            "credential_ref": "env:POSTGRES_TARGET_URL",
        },
    }

    context = build_agent_input_context(config, server_info)
    serialized = json.dumps(context, default=str)

    assert "password" not in serialized.lower()
    assert context["database_environment"]["environment_name"] == "dev"
    assert context["database_environment"]["target_database"] == "db_agent"
    assert context["database_environment"]["safe_host_label"] == "127.0.0.1"
    assert context["database_environment"]["allow_write_tools"] is True
    assert context["runtime_policy"]["allow_database_writes"] is True


def test_connection_card_prefers_server_target_metadata(tmp_path):
    config = CliRuntimeConfig(
        workspace=str(tmp_path),
        database_url="postgresql://cli_user:secret@localhost:5432/cli_db",
        target_environment="unknown",
    )
    server_info = {
        "postgres": {"target_configured": True},
        "database_environment": {
            "environment_name": "dev",
            "target_database": "server_db",
            "safe_host_label": "db.example.com",
            "safe_user_label": "server_user",
        },
    }

    card = build_db_connection_card(config, server_info)

    assert card["source"] == "server"
    assert card["host"] == "db.example.com"
    assert card["database"] == "server_db"
    assert card["user"] == "server_user"
    assert card["display_url"] == "server env:POSTGRES_TARGET_URL"


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


def test_slash_command_completer_suggests_commands():
    from prompt_toolkit.document import Document

    completions = list(SlashCommandCompleter().get_completions(Document("/do"), None))

    assert any(item.text == "/doctor" for item in completions)
    assert all(item.text.startswith("/do") for item in completions)


def test_parse_args_supports_verbose_flag(tmp_path):
    args = parse_args(["--workspace", str(tmp_path), "--verbose", "sessions"])
    config = runtime_config_from_args(args)

    assert config.verbose is True


def test_default_cli_start_creates_new_session_even_when_resume_index_exists(tmp_path):
    args = parse_args(["--workspace", str(tmp_path)])
    config = runtime_config_from_args(args)

    assert _select_thread_id(args, config) is None

    resume_args = parse_args(["--workspace", str(tmp_path), "--resume", "thread-1"])
    resume_config = runtime_config_from_args(resume_args)

    assert _select_thread_id(resume_args, resume_config) == "thread-1"


@pytest.mark.asyncio
async def test_resume_without_thread_id_uses_interactive_picker(tmp_path, monkeypatch):
    monkeypatch.setenv("MINIDB_AGENT_HOME", str(tmp_path / "home"))
    config = CliRuntimeConfig(
        workspace=str(tmp_path),
        database_url="postgresql://u:p@localhost:5432/appdb",
        target_environment="dev",
    )
    SessionIndex().upsert(record_from_runtime(config, thread_id="thread-1", title="检查慢 SQL"))

    async def fake_choose_session(records):
        assert records[0]["thread_id"] == "thread-1"
        return "thread-1"

    monkeypatch.setattr("cli.session_picker.choose_session", fake_choose_session)

    assert await _pick_thread_id(config) == "thread-1"


def test_session_label_is_compact_and_human_readable():
    label = session_label(
        {
            "thread_id": "thread-1234567890",
            "title": "检查慢 SQL 并输出诊断报告",
            "target_environment": "dev",
            "last_status": "completed",
            "updated_at": "2026-06-13T10:00:00Z",
        }
    )

    assert "检查慢 SQL" in label
    assert "2026-06-13 10:00" in label
    assert "dev/completed" not in label
    assert "thread-12345" not in label
    assert len(label.splitlines()) == 1


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


def test_session_index_ignores_empty_fallback_records(tmp_path):
    index = SessionIndex(tmp_path / "sessions.json")
    config = CliRuntimeConfig(workspace=str(tmp_path), save_session=True)
    empty = record_from_runtime(config, thread_id="thread-empty")

    assert has_resume_content(empty) is False
    index.upsert(empty)
    assert index.list() == []


def test_exec_exit_code_prefers_final_ready_state_over_recovered_blocked_event():
    values = {
        "db_task_runtime": {"task_status": "completed"},
        "delivery_packages": [{"status": "ready", "title": "慢 SQL 诊断"}],
    }
    events = [
        {"type": "blocked", "summary": "llm_output_error recovered"},
        {"type": "delivery_ready", "summary": "Delivery ready"},
    ]

    assert _exit_code_from_state(values, events) == 0


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
        self.stream_inputs = []
        self.stream_configs = []
        self.joined = []
        self.list_result = [{"run_id": "run-1", "status": "running"}]

    async def stream(self, **kwargs):
        self.stream_inputs.append(kwargs.get("input"))
        self.stream_configs.append(kwargs.get("config"))
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
        return self.list_result

    async def join(self, thread_id, run_id):
        self.joined.append({"thread_id": thread_id, "run_id": run_id})
        return {"__error__": {"error": "GraphRecursionError", "message": "limit reached"}}

    async def cancel(self, thread_id, run_id, wait=False, action="interrupt"):
        self.cancelled.append({"thread_id": thread_id, "run_id": run_id, "wait": wait, "action": action})


class _FakeThreads:
    def __init__(self):
        self.created = []

    async def create(self, thread_id=None):
        self.created.append(thread_id)
        return {"thread_id": thread_id or "thread-exec"}

    async def get(self, thread_id):
        return {"thread_id": thread_id}

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


@pytest.mark.asyncio
async def test_repl_new_session_lets_server_generate_thread_id(tmp_path):
    repl = AgentRepl(
        runtime_config=CliRuntimeConfig(server_url="http://server", workspace=str(tmp_path)),
        thread_id=None,
    )
    fake_client = _FakeClient()
    repl.client = fake_client

    await repl._ensure_thread()

    assert repl.thread_id == "thread-exec"
    assert fake_client.threads.created == [None]


@pytest.mark.asyncio
async def test_repl_resume_command_without_arg_uses_picker(tmp_path, monkeypatch):
    config = CliRuntimeConfig(
        workspace=str(tmp_path),
        database_url="postgresql://u:p@localhost:5432/appdb",
        target_environment="dev",
    )
    repl = AgentRepl(runtime_config=config, thread_id="current-thread")
    repl.client = _FakeClient()
    repl.session_index = SessionIndex(tmp_path / "sessions.json")
    repl.session_index.upsert(record_from_runtime(config, thread_id="current-thread", title="current"))
    repl.session_index.upsert(record_from_runtime(config, thread_id="old-thread", title="old"))

    async def fake_choose_session(records):
        assert all(record["thread_id"] != "current-thread" for record in records)
        return "old-thread"

    monkeypatch.setattr("cli.repl.choose_session", fake_choose_session)

    handled = await repl._handle_command("/resume")

    assert handled is True
    assert repl.thread_id == "old-thread"
    assert repl.event_adapter.thread_id == "old-thread"


@pytest.mark.asyncio
async def test_repl_resume_picker_does_not_fallback_to_current_session(tmp_path, monkeypatch):
    config = CliRuntimeConfig(
        workspace=str(tmp_path),
        database_url="postgresql://u:p@localhost:5432/appdb",
        target_environment="dev",
    )
    repl = AgentRepl(runtime_config=config, thread_id="current-thread")
    repl.session_index = SessionIndex(tmp_path / "sessions.json")
    repl.session_index.upsert(record_from_runtime(config, thread_id="current-thread", title="current"))

    async def fake_choose_session(records):
        assert records == []
        return None

    monkeypatch.setattr("cli.repl.choose_session", fake_choose_session)

    assert await repl._select_session_to_resume() is None


@pytest.mark.asyncio
async def test_reconnectdb_updates_config_and_starts_new_session(tmp_path, monkeypatch):
    monkeypatch.setenv("MINIDB_AGENT_HOME", str(tmp_path / "home"))
    initial = CliRuntimeConfig(
        server_url="http://server",
        workspace=str(tmp_path),
        database_url="postgresql://u:p@localhost:5432/old",
        target_environment="dev",
    )
    repl = AgentRepl(runtime_config=initial, thread_id="old-thread")
    fake_client = _FakeClient()
    repl.client = fake_client

    new_config = CliRuntimeConfig(
        server_url="http://server2",
        workspace=str(tmp_path),
        database_url="postgresql://u:p@localhost:5432/new",
        target_environment="staging",
    )

    async def fake_ensure_local_server(config, force_restart=False):
        assert force_restart is True
        assert config.database_url.endswith("/new")
        return new_config

    monkeypatch.setattr("cli.repl.prompt_reconnect_config", lambda config: new_config)
    monkeypatch.setattr("cli.repl.ensure_local_server", fake_ensure_local_server)
    async def fake_fetch_agent_info(config):
        return None

    monkeypatch.setattr("cli.repl.fetch_agent_info", fake_fetch_agent_info)
    monkeypatch.setattr("cli.repl.get_client", lambda url, api_key=None: _FakeClient())

    await repl._cmd_reconnectdb()

    assert repl.runtime_config.database_url.endswith("/new")
    assert repl.server_url == "http://server2"
    assert repl.thread_id == "thread-exec"
    assert repl.event_adapter.thread_id == "thread-exec"


@pytest.mark.asyncio
async def test_repl_applies_sql_approval_response_to_next_stream(tmp_path, monkeypatch):
    config = CliRuntimeConfig(workspace=str(tmp_path), target_environment="dev")
    repl = AgentRepl(runtime_config=config, thread_id="thread-1")
    fake_client = _FakeClient()
    repl.client = fake_client
    repl._assistant_id = "agent"
    repl._last_approval_card = {
        "approval_id": "approval-1",
        "target_environment": "dev",
        "database": "db_agent",
        "risk_level": "high",
        "classification": "data_change",
        "sql_hash": "hash-1",
        "sql_preview": "UPDATE orders SET status='done' WHERE id=1",
    }

    monkeypatch.setattr(
        "cli.repl.prompt_sql_approval",
        lambda card: {"action": "approve", "approval_id": card["approval_id"], "sql_hash": card["sql_hash"]},
    )

    await repl._handle_pending_approval()

    assert fake_client.runs.stream_inputs[-1]["cli_approval_response"] == {
        "action": "approve",
        "approval_id": "approval-1",
        "sql_hash": "hash-1",
    }


def test_repl_default_tool_output_is_compact_and_hides_raw_json(tmp_path, capsys):
    repl = AgentRepl(runtime_config=CliRuntimeConfig(workspace=str(tmp_path)))
    content = dumps_result(
        make_result(
            tool_name="postgres_connection_check",
            success=True,
            result_type="connection_status",
            summary="PostgreSQL connection is available.",
            payload={"database": "db_agent"},
        )
    )

    repl._process_messages(
        [{"type": "tool", "name": "postgres_connection_check", "content": content}],
        is_tool=True,
    )

    out = capsys.readouterr().out
    assert "postgres_connection_check" in out
    assert "PostgreSQL connection is available" in out
    assert "mini_agent_postgres_tool_result" not in out


@pytest.mark.asyncio
async def test_exec_mode_passes_recursion_limit_to_run_stream(tmp_path):
    fake_client = _FakeClient()
    config = CliRuntimeConfig(
        server_url="http://server",
        workspace=str(tmp_path),
        output_mode="json",
        output_file=str(tmp_path / "result.json"),
        save_session=False,
        recursion_limit=99,
    )

    code = await run_exec(config, "检查索引", client_factory=lambda **kwargs: fake_client)

    assert code == 0
    assert fake_client.runs.stream_configs[-1] == {"recursion_limit": 99}


@pytest.mark.asyncio
async def test_exec_mode_reports_run_error_instead_of_silent_running(tmp_path):
    fake_client = _FakeClient()
    fake_client.runs.list_result = [{"run_id": "run-error", "status": "error"}]
    output_file = tmp_path / "result.json"
    config = CliRuntimeConfig(
        server_url="http://server",
        workspace=str(tmp_path),
        output_mode="json",
        output_file=str(output_file),
        save_session=False,
    )

    code = await run_exec(config, "检查索引", client_factory=lambda **kwargs: fake_client)
    payload = json.loads(output_file.read_text(encoding="utf-8"))

    assert code == 1
    assert payload["status"] == "error"
    assert payload["run_error"]["error"] == "GraphRecursionError"
    assert fake_client.runs.joined[-1]["run_id"] == "run-error"


def test_apply_cli_approval_response_clears_pending_and_allows_continue():
    state = {
        "messages": [],
        "pending_approval": {
            "id": "approval-1",
            "step_id": "execute-change",
            "status": "pending",
            "risk_level": "high",
            "target_environment": "dev",
            "sql_preview": "UPDATE orders SET status='done' WHERE id=1",
            "sql_hash": "hash-1",
            "impact_summary": "Update one row",
            "rollback_summary": "Restore old value",
            "user_message": None,
            "created_at": "2026-06-13T00:00:00Z",
            "resolved_at": None,
        },
        "cli_approval_response": {
            "action": "approve",
            "approval_id": "approval-1",
            "sql_hash": "hash-1",
        },
    }

    update = apply_cli_approval_response(state)

    assert update["pending_approval"] is None
    assert update["cli_approval_response"] is None
    assert update["loop_status"] == "running"
    assert update["approval_decisions"][0]["status"] == "approved"
