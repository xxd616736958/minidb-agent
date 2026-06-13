"""Tests for artifact generation and delivery packages."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.context import build_prompt_context
from agent.nodes.final_report import final_report
from delivery.manager import DeliveryManager
from quality.manager import QualityManager
from state_management.migration import StateMigration
from state_management.validator import StateValidator


pytestmark = pytest.mark.delivery


def _step(**extra):
    step = {
        "id": "observe",
        "description": "Collect EXPLAIN and schema evidence",
        "status": "completed",
        "dependencies": [],
        "result": "Seq scan on orders",
        "error": None,
        "phase": "observe",
        "operation_type": "diagnostic",
        "risk_level": "low",
        "requires_approval": False,
        "requires_rollback_plan": False,
        "evidence_required": ["explain_plan"],
        "success_criteria": ["EXPLAIN is available"],
        "expected_tools": ["postgres_explain"],
        "tool_policy": "read_only_tools",
    }
    step.update(extra)
    return step


def _state(tmp_path: Path, **extra):
    task_root = tmp_path / ".mini_agent" / "tasks" / "plan-1"
    task_root.mkdir(parents=True, exist_ok=True)
    for name in ("sql", "explain", "health", "approvals", "logs", "reports"):
        (task_root / name).mkdir(exist_ok=True)
    step = _step()
    state = {
        "messages": [],
        "session_id": "session-1",
        "workspace_profile": {
            "root_path": str(tmp_path),
            "read_allowed_paths": [str(tmp_path)],
            "write_allowed_paths": [str(tmp_path)],
            "artifact_root": str(tmp_path / ".mini_agent" / "artifacts"),
            "report_root": str(tmp_path / ".mini_agent" / "reports"),
            "temp_root": str(tmp_path / ".mini_agent" / "tmp"),
            "default_cwd": str(tmp_path),
            "git_repo": None,
            "dirty_state_known": False,
        },
        "database_environment": {
            "environment_name": "staging",
            "target_database": "app",
            "safe_host_label": "db.local",
            "safe_user_label": "agent",
            "access_mode": "diagnostic",
            "is_production": False,
            "default_statement_timeout_ms": 5000,
            "default_lock_timeout_ms": 1000,
            "max_result_rows": 200,
            "allow_write_tools": False,
            "require_backup_check_for_writes": True,
            "credential_ref": "env:POSTGRES_TARGET_URL",
        },
        "task_workspace": {
            "task_id": "plan-1",
            "intent_id": "intent-1",
            "plan_id": "plan-1",
            "root_path": str(task_root),
            "artifact_ids": [],
            "report_paths": [],
            "sql_draft_paths": [],
            "execution_log_ref": None,
            "created_at": "now",
            "updated_at": "now",
        },
        "current_intent": {
            "id": "intent-1",
            "domain": "postgresql",
            "primary_intent": "performance_diagnosis",
            "candidate_intents": ["performance_diagnosis"],
            "confidence": 0.9,
            "goal": "Diagnose slow orders query",
            "user_language_summary": "Diagnose slow orders query",
            "operation_nature": "diagnostic",
            "target_environment": "staging",
            "target_database": "app",
            "target_objects": [{"type": "table", "name": "orders"}],
            "input_artifacts": [],
            "output_contract": {},
            "missing_slots": [],
            "assumptions": [],
            "constraints": ["read-only"],
            "risk_level": "low",
            "requires_clarification": False,
            "requires_approval": False,
            "requires_rollback_plan": False,
            "evidence_needed": ["execution_plan"],
            "suggested_workflow": "performance_diagnosis_workflow",
            "next_action": "read_only_observe",
        },
        "db_task_plan": {
            "id": "plan-1",
            "intent_id": "intent-1",
            "workflow": "performance_diagnosis_workflow",
            "summary": "Diagnose slow orders query",
            "status": "completed",
            "steps": [step],
            "assumptions": [],
            "constraints": ["read-only"],
            "global_risk_level": "low",
            "requires_user_confirmation": False,
            "created_at": "now",
            "updated_at": "now",
        },
        "task_stack": [step],
        "current_task_index": 0,
        "current_step_id": None,
        "loop_status": "completed",
        "db_observations": [
            {
                "id": "obs-explain",
                "step_id": "observe",
                "type": "explain_plan",
                "source_tool": "postgres_explain",
                "summary": "Seq Scan on orders",
                "payload": {"plan": "Seq Scan"},
                "created_at": "now",
            }
        ],
        "tool_execution_results": [],
        "artifact_records": [],
        "approval_decisions": [],
        "verification_results": [
            {
                "id": "verify-1",
                "step_id": "observe",
                "status": "passed",
                "criteria_checked": ["EXPLAIN is available"],
                "evidence_ids": ["obs-explain"],
                "summary": "EXPLAIN evidence is available.",
                "created_at": "now",
            }
        ],
        "quality_gates": [],
    }
    state.update(extra)
    return state


def test_delivery_contract_for_performance_report(tmp_path):
    contract = DeliveryManager(_state(tmp_path)).build_contract()

    assert contract["delivery_mode"] == "artifact_package"
    assert "diagnostic_report" in contract["required_items"]
    assert "explain_plan" in contract["required_evidence_types"]
    assert contract["requires_sql_package"] is False


def test_manifest_collects_evidence_and_sql_metadata(tmp_path):
    state = _state(
        tmp_path,
        approval_decisions=[
            {
                "id": "approval-1",
                "step_id": "execute",
                "status": "approved",
                "risk_level": "high",
                "target_environment": "staging",
                "sql_preview": "ALTER TABLE orders ADD COLUMN note text",
                "sql_hash": "hash-1",
                "impact_summary": "Adds nullable column",
                "rollback_summary": "ALTER TABLE orders DROP COLUMN note",
                "user_message": "approved",
                "created_at": "now",
                "resolved_at": "now",
            }
        ],
        sql_safety_reports=[
            {
                "id": "sql-report-1",
                "sql": "ALTER TABLE orders ADD COLUMN note text",
                "sql_hash": "hash-1",
                "classification": "schema_change",
                "risk_level": "high",
                "allowed": True,
                "reasons": [],
                "created_at": "now",
            }
        ],
    )

    manifest = DeliveryManager(state).build_manifest()

    assert any(ref["source_type"] == "db_observation" for ref in manifest["evidence_refs"])
    assert manifest["sql_items"][0]["approval_id"] == "approval-1"
    assert manifest["sql_items"][0]["safety_report_id"] == "sql-report-1"


def test_manifest_handles_approval_without_safety_report(tmp_path):
    state = _state(
        tmp_path,
        approval_decisions=[
            {
                "id": "approval-1",
                "step_id": "observe",
                "status": "approved",
                "risk_level": "high",
                "target_environment": "staging",
                "sql_preview": "ALTER TABLE orders ADD COLUMN note text",
                "sql_hash": "hash-1",
                "impact_summary": "Adds nullable column",
                "rollback_summary": "ALTER TABLE orders DROP COLUMN note",
                "user_message": "approved",
                "created_at": "now",
                "resolved_at": "now",
            }
        ],
        sql_safety_reports=[],
    )

    manifest = DeliveryManager(state).build_manifest()

    assert manifest["sql_items"][0]["approval_id"] == "approval-1"
    assert manifest["sql_items"][0]["safety_report_id"] is None


def test_delivery_quality_gate_allows_draft_write_sql_without_execution_approval(tmp_path):
    state = _state(
        tmp_path,
        current_intent={**_state(tmp_path)["current_intent"], "operation_nature": "schema_change", "requires_approval": True, "requires_rollback_plan": True},
        sql_safety_reports=[
            {
                "id": "sql-report-1",
                "sql": "ALTER TABLE orders ADD COLUMN note text",
                "sql_hash": "hash-1",
                "classification": "schema_change",
                "risk_level": "high",
                "allowed": True,
                "reasons": [],
                "created_at": "now",
            }
        ],
    )
    manager = DeliveryManager(state)
    contract = manager.build_contract()
    manifest = manager.build_manifest()
    gate = manager.delivery_quality_gate(contract, manifest, report_paths=["/tmp/final_report.md"])

    assert gate["status"] == "passed"
    assert "write_items_have_approval" not in gate["failed_checks"]
    assert "approve_execution" in manager.next_actions(contract, manifest, manifest["missing_items"])


def test_delivery_accepts_call_id_as_sql_safety_evidence(tmp_path):
    state = _state(
        tmp_path,
        current_intent={
            **_state(tmp_path)["current_intent"],
            "operation_nature": "schema_change",
            "requires_approval": True,
            "requires_rollback_plan": True,
        },
        tool_invocation_records=[
            {"call_id": "call-classify", "step_id": "observe"},
        ],
        sql_safety_reports=[
            {
                "call_id": "call-classify",
                "tool_call_id": "call-classify",
                "step_id": "observe",
                "sql": "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_orders_status ON orders (status);",
                "sql_hash": "hash-1",
                "classification": "schema_change",
                "risk_level": "high",
                "allowed": True,
                "reasons": [],
                "created_at": "now",
            }
        ],
    )
    manager = DeliveryManager(state)
    contract = manager.build_contract()
    manifest = manager.build_manifest()
    gate = manager.delivery_quality_gate(contract, manifest, report_paths=["/tmp/final_report.md"])

    assert manifest["sql_items"][0]["safety_report_id"] == "call-classify"
    assert gate["status"] == "passed"


def test_diagnostic_only_sql_items_do_not_require_approval_package(tmp_path):
    base = _state(tmp_path)
    state = _state(
        tmp_path,
        current_intent={
            **base["current_intent"],
            "operation_nature": "schema_change",
            "requires_approval": True,
            "requires_rollback_plan": True,
        },
        sql_safety_reports=[
            {
                "id": "sql-report-readonly",
                "sql": "EXPLAIN SELECT * FROM orders",
                "sql_hash": "hash-readonly",
                "classification": "read_only",
                "risk_level": "low",
                "allowed": True,
                "reasons": [],
                "created_at": "now",
            }
        ],
    )

    manager = DeliveryManager(state)
    contract = manager.build_contract()
    manifest = manager.build_manifest()
    gate = manager.delivery_quality_gate(contract, manifest, report_paths=["/tmp/final_report.md"])

    assert "approval_package" not in manifest["missing_items"]
    assert "write_items_have_approval" not in gate["failed_checks"]

    manifest_without_safety_id = {
        **manifest,
        "sql_items": [
            {
                **manifest["sql_items"][0],
                "safety_report_id": None,
            }
        ],
    }
    gate_without_safety_id = manager.delivery_quality_gate(
        contract,
        manifest_without_safety_id,
        report_paths=["/tmp/final_report.md"],
    )
    assert "sql_items_have_safety_metadata" not in gate_without_safety_id["failed_checks"]


def test_delivery_filters_evidence_and_sql_to_current_plan(tmp_path):
    state = _state(
        tmp_path,
        db_task_plan={
            **_state(tmp_path)["db_task_plan"],
            "id": "plan-current",
            "steps": [_step(id="current-step")],
        },
        task_stack=[_step(id="current-step")],
        task_workspace={
            **_state(tmp_path)["task_workspace"],
            "task_id": "plan-current",
            "plan_id": "plan-current",
        },
        db_observations=[
            {
                "id": "obs-old",
                "step_id": "old-step",
                "type": "top_queries",
                "source_tool": "postgres_top_queries",
                "summary": "Old top query",
                "payload": {"queries": [{"query_preview": "SELECT old"}]},
                "created_at": "now",
            },
            {
                "id": "obs-current",
                "step_id": "current-step",
                "type": "object_detail",
                "source_tool": "postgres_object_detail",
                "summary": "Current object detail",
                "payload": {"columns": [{"column_name": "id"}]},
                "created_at": "now",
            },
        ],
        tool_invocation_records=[
            {"call_id": "call-old", "step_id": "old-step"},
            {"call_id": "call-current", "step_id": "current-step"},
        ],
        tool_execution_results=[
            {
                "tool_call_id": "call-old",
                "tool_name": "postgres_sql_classify",
                "success": True,
                "result_type": "sql_classification",
                "summary": "Old DDL",
                "payload": {
                    "sql": "CREATE INDEX idx_old ON old_table (id);",
                    "sql_hash": "hash-old",
                    "primary_type": "schema_change",
                    "risk_level": "high",
                },
            },
            {
                "tool_call_id": "call-current",
                "tool_name": "postgres_query_readonly",
                "success": True,
                "result_type": "query_result",
                "summary": "Current rows",
                "payload": {"rows": [{"id": 1}]},
            },
        ],
        sql_safety_reports=[
            {
                "id": "sql-old",
                "call_id": "call-old",
                "sql": "CREATE INDEX idx_old ON old_table (id);",
                "sql_hash": "hash-old",
                "classification": "schema_change",
                "risk_level": "high",
            }
        ],
        verification_results=[
            {
                "id": "verify-current",
                "step_id": "current-step",
                "status": "passed",
                "criteria_checked": [],
                "evidence_ids": ["obs-current"],
                "summary": "current done",
                "created_at": "now",
            }
        ],
    )

    manifest = DeliveryManager(state).build_manifest()

    assert [ref["source_id"] for ref in manifest["evidence_refs"] if ref["source_type"] == "db_observation"] == ["obs-current"]
    assert manifest["sql_items"] == []


def test_final_report_node_writes_reports_and_package(tmp_path):
    update = final_report(_state(tmp_path))

    package = update["delivery_packages"][0]
    assert package["status"] == "ready"
    assert Path(package["user_report_path"]).exists()
    assert Path(package["audit_report_path"]).exists()
    assert update["quality_gates"][0]["gate_type"] == "delivery_quality"
    assert update["artifact_records"]
    assert "final_report_shown" == update["collaboration_events"][0]["event_type"]


def test_final_report_uses_runtime_completed_over_stale_loop_status(tmp_path):
    state = _state(
        tmp_path,
        loop_status="blocked",
        db_task_runtime={"task_status": "completed", "risk_level": "low"},
    )

    update = final_report(state)
    package = update["delivery_packages"][0]
    report = Path(package["user_report_path"]).read_text(encoding="utf-8")

    assert package["status"] == "ready"
    assert "Delivery status: ready" in report


def test_final_report_does_not_skip_new_plan_when_previous_package_exists(tmp_path):
    state = _state(
        tmp_path,
        active_delivery_contract={"id": "contract-old", "plan_id": "plan-old", "status": "ready"},
        delivery_packages=[
            {
                "id": "package-old",
                "task_id": "task-old",
                "contract_id": "contract-old",
                "title": "old task",
                "status": "ready",
                "summary": "old delivery",
                "user_report_path": None,
                "audit_report_path": None,
                "manifest_id": "manifest-old",
                "artifact_ids": [],
                "quality_gate_ids": [],
                "next_actions": [],
                "created_at": "now",
                "delivered_at": "now",
            }
        ],
        db_task_runtime={"task_status": "completed", "risk_level": "low"},
    )

    update = final_report(state)

    assert update["delivery_packages"][0]["id"] != "package-old"
    assert update["active_delivery_contract"]["plan_id"] == "plan-1"


def test_final_report_treats_recovered_report_error_as_ready_delivery(tmp_path):
    state = _state(
        tmp_path,
        error_reports=[
            {
                "id": "error-report-1",
                "task_id": "plan-1",
                "plan_id": "plan-1",
                "step_id": "report-findings",
                "status": "recovered",
                "error_ids": ["err-1"],
                "recovery_attempt_ids": ["attempt-1"],
                "evidence_refs": ["obs-explain"],
                "user_summary": "LLM report timed out; delivered from structured evidence.",
                "next_options": ["review_final_report"],
                "created_at": "now",
            }
        ],
    )

    update = final_report(state)
    package = update["delivery_packages"][0]
    report = Path(package["user_report_path"]).read_text(encoding="utf-8")

    assert package["status"] == "ready"
    assert "Delivery status: ready" in report
    assert "recovered: LLM report timed out" in report


def test_final_report_includes_top_query_findings(tmp_path):
    top_query_step = _step(
        id="collect-top-queries",
        description="Collect top query evidence",
        expected_tools=["postgres_top_queries"],
    )
    state = _state(
        tmp_path,
        db_task_plan={
            **_state(tmp_path)["db_task_plan"],
            "steps": [top_query_step],
        },
        task_stack=[top_query_step],
        db_observations=[
            {
                "id": "obs-top",
                "step_id": "collect-top-queries",
                "type": "top_queries",
                "source_tool": "postgres_top_queries",
                "summary": "Collected 2 top query row(s) ordered by resources.",
                "payload": {
                    "sort_by": "resources",
                    "history_available": True,
                    "queries": [
                        {
                            "query_preview": "INSERT INTO big_orders_demo SELECT generate_series($1, $2)",
                            "calls": 10,
                            "rows": 5000000,
                            "total_exec_time": 35158.9,
                            "mean_exec_time": 3515.9,
                            "shared_blks_read": 4,
                            "temp_blks_read": 8550,
                            "temp_blks_written": 8550,
                        },
                        {
                            "query_preview": "SELECT COUNT(*) FROM big_orders_demo",
                            "calls": 4,
                            "rows": 4,
                            "total_exec_time": 22737.9,
                            "mean_exec_time": 5684.4,
                            "shared_blks_read": 2355376,
                            "temp_blks_read": 0,
                            "temp_blks_written": 0,
                        },
                    ],
                },
                "created_at": "now",
            }
        ],
    )

    update = final_report(state)
    report = Path(update["delivery_packages"][0]["user_report_path"]).read_text(encoding="utf-8")

    assert "## 主要发现" in report
    assert "SELECT COUNT(*) FROM big_orders_demo" in report
    assert "Total time: 22.74s" in report
    assert "exact full-table count pattern" in report


def test_final_report_uses_aggregate_hint_for_grouped_top_query(tmp_path):
    top_query_step = _step(
        id="collect-top-queries",
        description="Collect top query evidence",
        expected_tools=["postgres_top_queries"],
    )
    state = _state(
        tmp_path,
        db_task_plan={
            **_state(tmp_path)["db_task_plan"],
            "steps": [top_query_step],
        },
        task_stack=[top_query_step],
        db_observations=[
            {
                "id": "obs-top",
                "step_id": "collect-top-queries",
                "type": "top_queries",
                "source_tool": "postgres_top_queries",
                "summary": "Collected 1 top query row(s) ordered by resources.",
                "payload": {
                    "sort_by": "resources",
                    "history_available": True,
                    "queries": [
                        {
                            "query_preview": "SELECT status, COUNT(*) AS cnt, COUNT(DISTINCT user_id) FROM public.big_orders_demo GROUP BY status ORDER BY cnt DESC",
                            "calls": 1,
                            "rows": 5,
                            "total_exec_time": 13430.0,
                            "mean_exec_time": 13430.0,
                            "shared_blks_read": 4491429,
                            "temp_blks_read": 15439,
                            "temp_blks_written": 15493,
                        },
                    ],
                },
                "created_at": "now",
            }
        ],
    )

    update = final_report(state)
    report = Path(update["delivery_packages"][0]["user_report_path"]).read_text(encoding="utf-8")

    assert "aggregate/grouping workload" in report
    assert "temporary spill" in report
    assert "exact full-table count pattern" not in report


def test_final_report_filters_internal_control_messages_from_summary(tmp_path):
    state = _state(
        tmp_path,
        messages=[
            {
                "role": "system",
                "content": "The current report step does not allow additional tool calls. Blocked tools: postgres_query_readonly",
            },
            {
                "type": "ai",
                "content": "The current report step does not allow additional tool calls. Blocked tools: postgres_query_readonly",
            },
        ],
    )

    update = final_report(state)
    report = Path(update["delivery_packages"][0]["user_report_path"]).read_text(encoding="utf-8")

    assert "does not allow additional tool calls" not in report
    assert "Blocked tools:" not in report


def test_final_report_renders_assistant_report_as_separate_section(tmp_path):
    state = _state(
        tmp_path,
        messages=[
            {
                "type": "ai",
                "content": "## 性能诊断报告\n\n```sql\nSELECT COUNT(*) FROM orders;\n```\n\n- 建议先验证执行计划",
            },
        ],
    )

    update = final_report(state)
    report = Path(update["delivery_packages"][0]["user_report_path"]).read_text(encoding="utf-8")

    assert "## 摘要\n\nDiagnose slow orders query" in report
    assert "## 助手结论" in report
    assert "```sql\nSELECT COUNT(*) FROM orders;\n```" in report


def test_context_migration_validator_and_quality_report_include_delivery(tmp_path):
    state = _state(tmp_path)
    migration = StateMigration(state).migrate()

    assert migration["delivery_contracts"] == []
    assert migration["delivery_packages"] == []

    update = final_report(state)
    merged = {**state, **update}
    context, _ = build_prompt_context({**merged, "context_token_budget": 1400})
    report = StateValidator(merged).validate()
    quality = QualityManager(merged).quality_report(
        target_ref="plan-1",
        scope="task",
        gates=merged["quality_gates"],
        evaluation_results=[],
    )

    assert "Artifact Generation and Delivery" in context
    assert report["ok"] is True
    assert quality["delivery_summary"]["delivery_packages"] == 1


def test_validator_rejects_ready_package_without_report(tmp_path):
    state = _state(tmp_path)
    manager = DeliveryManager(state)
    contract = manager.build_contract()
    manifest = manager.build_manifest()
    package = manager.delivery_package(contract, manifest, QualityManager.gate(
        gate_type="delivery_quality",
        target_ref="manifest",
        required_checks=[],
        blocking=False,
    ), [], [], status="ready")

    report = StateValidator(
        {
            **state,
            "delivery_contracts": [contract],
            "artifact_manifests": [manifest],
            "delivery_packages": [package],
        }
    ).validate()

    assert report["ok"] is False
    assert any("user_report_path" in error for error in report["errors"])
