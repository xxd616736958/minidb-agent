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


def test_delivery_quality_gate_blocks_missing_required_write_approval(tmp_path):
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

    assert gate["status"] == "failed"
    assert "write_items_have_approval" in gate["failed_checks"]


def test_final_report_node_writes_reports_and_package(tmp_path):
    update = final_report(_state(tmp_path))

    package = update["delivery_packages"][0]
    assert package["status"] == "ready"
    assert Path(package["user_report_path"]).exists()
    assert Path(package["audit_report_path"]).exists()
    assert update["quality_gates"][0]["gate_type"] == "delivery_quality"
    assert update["artifact_records"]
    assert "final_report_shown" == update["collaboration_events"][0]["event_type"]


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
