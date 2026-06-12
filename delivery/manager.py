"""Delivery contracts, artifact manifests, reports, and quality gates."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent.state import (
    AgentState,
    ArtifactManifest,
    ArtifactRecord,
    DeliveryContract,
    DeliveryPackage,
    EvidenceReference,
    QualityGate,
    ReportSection,
    SQLDeliveryItem,
)
from execution.environment import ArtifactStore, ExecutionEnvironmentManager
from quality.manager import QualityManager


SENSITIVE_RE = re.compile(
    r"(password\s*[:=]|token\s*[:=]|secret\s*[:=]|api[_-]?key\s*[:=]|[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}|phone\s*[:=]|id_card\s*[:=])",
    re.IGNORECASE,
)
WRITE_CLASSIFICATIONS = {"data_change", "schema_change", "permission_change", "maintenance", "transaction_control"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def compact(value: Any, limit: int = 500) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sensitivity_rank(value: str | None) -> int:
    return {"public": 1, "internal": 2, "sensitive": 3, "secret": 4}.get(str(value or "internal"), 2)


class DeliveryManager:
    """Build auditable delivery contracts, manifests, reports, and packages."""

    def __init__(self, state: AgentState | None = None) -> None:
        self.state = state or {}

    def build_contract(self) -> DeliveryContract:
        intent = self.state.get("current_intent") or {}
        plan = self.state.get("db_task_plan") or {}
        workflow = str(plan.get("workflow") or self.state.get("selected_workflow") or intent.get("suggested_workflow") or "")
        primary = str(intent.get("primary_intent") or "")
        operation = str(intent.get("operation_nature") or "")
        steps = plan.get("steps", []) or self.state.get("task_stack", [])
        has_write = any(
            step.get("operation_type") in {"schema_change", "data_change", "permission_change", "backup_restore", "maintenance"}
            for step in steps
        ) or operation in {"write_data", "schema_change", "permission_change", "backup_restore"}
        requires_approval = bool(intent.get("requires_approval") or has_write)
        requires_rollback = bool(intent.get("requires_rollback_plan") or has_write)
        requires_verification = bool(has_write or self.state.get("verification_results") or any(step.get("phase") == "verify" for step in steps))

        required_items = ["final_report", "artifact_manifest"]
        required_evidence = []
        delivery_mode = "artifact_package"
        audience = "dba" if intent.get("domain") == "postgresql" else "general"
        if "performance" in workflow or "performance" in primary:
            required_items.extend(["diagnostic_report", "evidence_summary"])
            required_evidence.extend(["explain_plan", "schema_summary"])
        if has_write:
            required_items.extend(["sql_change_package", "approval_package", "rollback_package", "verification_report"])
            required_evidence.extend(["sql_safety_report", "approval", "verification"])
            delivery_mode = "approval_package" if requires_approval else "audit_package"
        if self.state.get("loop_status") == "blocked" or self.state.get("error_reports"):
            required_items.append("blocked_report")
        if not required_evidence and intent.get("domain") == "postgresql":
            required_evidence.append("db_observation")

        sensitivity = self._state_sensitivity()
        return {
            "id": new_id("delivery-contract"),
            "intent_id": str(intent.get("id") or ""),
            "plan_id": plan.get("id"),
            "audience": audience,  # type: ignore[typeddict-item]
            "delivery_mode": delivery_mode,  # type: ignore[typeddict-item]
            "required_items": list(dict.fromkeys(required_items)),
            "optional_items": ["audit_report", "quality_report", "model_summary"],
            "required_evidence_types": list(dict.fromkeys(required_evidence)),
            "requires_sql_package": has_write,
            "requires_approval_evidence": requires_approval,
            "requires_rollback_plan": requires_rollback,
            "requires_verification": requires_verification,
            "output_formats": ["markdown", "json", "sql"],
            "sensitivity": sensitivity,  # type: ignore[typeddict-item]
            "status": "draft",
            "created_at": now_iso(),
            "updated_at": now_iso(),
        }

    def build_manifest(self) -> ArtifactManifest:
        task_workspace = self._task_workspace()
        evidence_refs = self.evidence_references()
        sql_items = self.sql_delivery_items()
        artifact_ids = [str(item.get("id")) for item in self.state.get("artifact_records", []) if item.get("id")]
        report_paths = list((task_workspace or {}).get("report_paths", []) or [])
        missing = self._missing_manifest_items(evidence_refs, sql_items)
        return {
            "id": new_id("artifact-manifest"),
            "task_id": str((task_workspace or {}).get("task_id") or self._task_id()),
            "artifact_ids": artifact_ids,
            "evidence_refs": evidence_refs,
            "sql_items": sql_items,
            "report_paths": report_paths,
            "missing_items": missing,
            "sensitivity": self._state_sensitivity(),  # type: ignore[typeddict-item]
            "created_at": now_iso(),
        }

    def evidence_references(self) -> list[EvidenceReference]:
        refs: list[EvidenceReference] = []
        for obs in self.state.get("db_observations", []):
            refs.append(self._evidence("db_observation", obs.get("id"), obs.get("summary"), f"Supports {obs.get('type')}", "internal"))
        for result in self.state.get("tool_execution_results", []):
            refs.append(self._evidence("tool_result", result.get("tool_call_id"), result.get("summary"), f"Supports tool result {result.get('result_type')}", "internal"))
        for artifact in self.state.get("artifact_records", []):
            refs.append(self._evidence("artifact", artifact.get("id"), artifact.get("summary"), f"Supports artifact {artifact.get('kind')}", artifact.get("sensitivity")))
        for approval in self.state.get("approval_decisions", []):
            refs.append(self._evidence("approval", approval.get("id"), approval.get("impact_summary") or approval.get("user_message"), f"Supports approval status {approval.get('status')}", "internal"))
        for verification in self.state.get("verification_results", []):
            refs.append(self._evidence("verification", verification.get("id"), verification.get("summary"), f"Supports verification status {verification.get('status')}", "internal"))
        for gate in self.state.get("quality_gates", []):
            refs.append(self._evidence("quality_gate", gate.get("id"), f"{gate.get('gate_type')} {gate.get('status')}", "Supports quality gate result", "internal"))
        for record in self.state.get("model_invocation_records", []):
            refs.append(self._evidence("model_record", record.get("id"), f"{record.get('task')} {record.get('model_id')} {record.get('status')}", "Supports model invocation audit", "internal"))
        for report in self.state.get("error_reports", []):
            refs.append(self._evidence("error_report", report.get("id"), report.get("user_summary"), "Supports blocked/error delivery", "internal"))
        return [ref for ref in refs if ref["source_id"]]

    def sql_delivery_items(self) -> list[SQLDeliveryItem]:
        approvals = self.state.get("approval_decisions", [])
        reports = self.state.get("sql_safety_reports", [])
        verification_ids = [str(item.get("id")) for item in self.state.get("verification_results", []) if item.get("id")]
        items: list[SQLDeliveryItem] = []
        for report in reports:
            sql = str(report.get("sql") or report.get("normalized_sql") or report.get("sql_preview") or "")
            sql_hash = str(report.get("sql_hash") or (sha256_text(sql) if sql else ""))
            classification = str(report.get("classification") or "unknown")
            approval = self._approval_for_sql(sql_hash, approvals)
            purpose = self._sql_purpose(classification, sql)
            items.append(
                {
                    "id": new_id("sql-delivery"),
                    "purpose": purpose,  # type: ignore[typeddict-item]
                    "sql_preview": compact(sql or report.get("summary"), 600),
                    "sql_hash": sql_hash,
                    "classification": classification,
                    "risk_level": str(report.get("risk_level") or (approval or {}).get("risk_level") or "unknown"),
                    "target_environment": self._target_environment(),
                    "approval_id": (approval or {}).get("id"),
                    "safety_report_id": report.get("id"),
                    "execution_record_id": self._execution_record_for_sql(sql_hash),
                    "verification_refs": verification_ids,
                    "status": self._sql_status(classification, approval, verification_ids),  # type: ignore[typeddict-item]
                }
            )
        for approval in approvals:
            sql = str(approval.get("sql_preview") or "")
            sql_hash = str(approval.get("sql_hash") or (sha256_text(sql) if sql else ""))
            if not sql_hash or any(item["sql_hash"] == sql_hash for item in items):
                continue
            items.append(
                {
                    "id": new_id("sql-delivery"),
                    "purpose": "change",
                    "sql_preview": compact(sql, 600),
                    "sql_hash": sql_hash,
                    "classification": "unknown",
                    "risk_level": str(approval.get("risk_level") or "unknown"),
                    "target_environment": str(approval.get("target_environment") or self._target_environment()),
                    "approval_id": approval.get("id"),
                    "safety_report_id": None,
                    "execution_record_id": self._execution_record_for_sql(sql_hash),
                    "verification_refs": verification_ids,
                    "status": "approved" if approval.get("status") == "approved" else "draft",
                }
            )
        return items

    def report_sections(self, contract: DeliveryContract, manifest: ArtifactManifest) -> list[ReportSection]:
        intent = self.state.get("current_intent") or {}
        plan = self.state.get("db_task_plan") or {}
        evidence_ids = [ref["id"] for ref in manifest["evidence_refs"][:12]]
        missing = manifest.get("missing_items", [])
        sections = [
            self._section(
                "summary",
                "摘要",
                compact(plan.get("summary") or intent.get("goal") or "Task delivery package generated.", 900),
                evidence_ids[:3],
                "complete" if intent or plan else "missing_evidence",
            ),
            self._section(
                "evidence",
                "证据",
                self._evidence_section(manifest),
                evidence_ids,
                "complete" if evidence_ids else "missing_evidence",
            ),
            self._section(
                "risk",
                "风险与安全",
                self._risk_section(manifest),
                evidence_ids,
                "complete",
            ),
            self._section(
                "verification",
                "验证",
                self._verification_section(),
                [ref["id"] for ref in manifest["evidence_refs"] if ref["source_type"] == "verification"],
                "complete" if self.state.get("verification_results") or not contract["requires_verification"] else "missing_evidence",
            ),
            self._section(
                "next_steps",
                "下一步",
                "\n".join(f"- {item}" for item in self.next_actions(contract, manifest, missing)),
                [],
                "complete",
            ),
        ]
        if contract["requires_sql_package"] or manifest["sql_items"]:
            sections.insert(
                2,
                self._section(
                    "execution",
                    "SQL 交付项",
                    self._sql_section(manifest["sql_items"]),
                    evidence_ids,
                    "complete" if manifest["sql_items"] else "missing_evidence",
                ),
            )
        if self.state.get("loop_status") == "blocked" or self.state.get("error_reports"):
            sections.insert(
                1,
                self._section(
                    "diagnosis",
                    "阻塞或失败说明",
                    self._blocked_section(),
                    [ref["id"] for ref in manifest["evidence_refs"] if ref["source_type"] == "error_report"],
                    "complete",
                ),
            )
        return sections

    def delivery_quality_gate(
        self,
        contract: DeliveryContract,
        manifest: ArtifactManifest,
        *,
        report_paths: list[str] | None = None,
    ) -> QualityGate:
        required = [
            "contract_satisfied",
            "required_evidence_present",
            "sql_items_have_safety_metadata",
            "write_items_have_approval",
            "rollback_present_when_required",
            "verification_present_when_required",
            "sensitive_data_redacted",
            "report_paths_recorded",
        ]
        passed: list[str] = []
        failed: list[str] = []
        missing = set(manifest.get("missing_items", []))
        (failed if missing & set(contract.get("required_items", [])) else passed).append("contract_satisfied")
        if contract.get("required_evidence_types") and not manifest.get("evidence_refs"):
            failed.append("required_evidence_present")
        else:
            passed.append("required_evidence_present")
        sql_items = manifest.get("sql_items", [])
        unsafe_sql = [item for item in sql_items if not item.get("sql_hash") or not item.get("safety_report_id")]
        (failed if unsafe_sql else passed).append("sql_items_have_safety_metadata")
        write_without_approval = [
            item for item in sql_items
            if item.get("classification") in WRITE_CLASSIFICATIONS and not item.get("approval_id")
        ]
        (failed if write_without_approval and contract.get("requires_approval_evidence") else passed).append("write_items_have_approval")
        rollback_ok = not contract.get("requires_rollback_plan") or any((approval.get("rollback_summary") for approval in self.state.get("approval_decisions", [])))
        (passed if rollback_ok else failed).append("rollback_present_when_required")
        verification_ok = not contract.get("requires_verification") or bool(self.state.get("verification_results"))
        (passed if verification_ok else failed).append("verification_present_when_required")
        (passed if not self._raw_secret_in_reports(report_paths or []) else failed).append("sensitive_data_redacted")
        (passed if report_paths else failed).append("report_paths_recorded")
        return QualityManager.gate(
            gate_type="delivery_quality",
            target_ref=str(manifest.get("id") or contract.get("id") or "delivery"),
            required_checks=required,
            passed_checks=list(dict.fromkeys(passed)),
            failed_checks=list(dict.fromkeys(failed)),
            blocking=bool(failed),
        )

    def build_delivery_update(self, *, force_blocked: bool = False) -> dict[str, Any]:
        env_update = ExecutionEnvironmentManager(self.state).bootstrap_state()
        delivery_state = {**self.state, **env_update}
        manager = DeliveryManager(delivery_state)
        contract = manager.build_contract()
        manifest = manager.build_manifest()
        sections = manager.report_sections(contract, manifest)
        report_paths, report_artifacts = manager.write_reports(contract, manifest, sections)
        manifest = {**manifest, "report_paths": [*manifest.get("report_paths", []), *report_paths]}
        manifest_path, manifest_artifact = manager.write_manifest(manifest)
        manifest = {**manifest, "report_paths": [*manifest.get("report_paths", []), manifest_path]}
        Path(manifest_path).write_text(json.dumps(manifest, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        report_paths = [*report_paths, manifest_path]
        gate = manager.delivery_quality_gate(contract, manifest, report_paths=report_paths)
        status = "blocked" if force_blocked or gate["status"] == "failed" or delivery_state.get("loop_status") == "blocked" else "ready"
        package = manager.delivery_package(contract, manifest, gate, report_paths, [*report_artifacts, manifest_artifact], status=status)
        contract = {**contract, "status": "blocked" if status in {"blocked", "failed"} else "ready", "updated_at": now_iso()}
        task_workspace = manager._task_workspace()
        if task_workspace:
            task_workspace = dict(task_workspace)
            task_workspace["artifact_ids"] = list(dict.fromkeys([
                *task_workspace.get("artifact_ids", []),
                *[artifact["id"] for artifact in [*report_artifacts, manifest_artifact]],
            ]))
            task_workspace["report_paths"] = list(dict.fromkeys([*task_workspace.get("report_paths", []), *report_paths]))
            task_workspace["updated_at"] = now_iso()
            env_update["task_workspace"] = task_workspace
        return {
            **env_update,
            "delivery_contracts": [contract],
            "active_delivery_contract": contract,
            "artifact_manifests": [manifest],
            "delivery_packages": [package],
            "report_sections": sections,
            "sql_delivery_items": manifest["sql_items"],
            "evidence_references": manifest["evidence_refs"],
            "quality_gates": [gate],
            "artifact_records": [*report_artifacts, manifest_artifact],
        }

    def write_reports(
        self,
        contract: DeliveryContract,
        manifest: ArtifactManifest,
        sections: list[ReportSection],
    ) -> tuple[list[str], list[ArtifactRecord]]:
        workspace = self._task_workspace()
        report_dir = Path(str(workspace.get("root_path") if workspace else ".mini_agent")) / "reports"
        report_dir.mkdir(parents=True, exist_ok=True)
        task_id = str((workspace or {}).get("task_id") or self._task_id())
        user_path = report_dir / "final_report.md"
        audit_path = report_dir / "audit_report.md"
        user_text = self.render_user_report(contract, manifest, sections)
        audit_text = self.render_audit_report(contract, manifest, sections)
        user_path.write_text(user_text, encoding="utf-8")
        audit_path.write_text(audit_text, encoding="utf-8")
        store = ArtifactStore(workspace)
        artifacts = [
            store.record(kind="final_report", path=str(user_path), summary=f"Final delivery report for {task_id}", lifecycle="persistent"),
            store.record(kind="audit_report", path=str(audit_path), summary=f"Audit delivery report for {task_id}", lifecycle="persistent"),
        ]
        return [str(user_path), str(audit_path)], artifacts

    def write_manifest(self, manifest: ArtifactManifest) -> tuple[str, ArtifactRecord]:
        workspace = self._task_workspace()
        report_dir = Path(str(workspace.get("root_path") if workspace else ".mini_agent")) / "reports"
        report_dir.mkdir(parents=True, exist_ok=True)
        path = report_dir / "manifest.json"
        path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        artifact = ArtifactStore(workspace).record(
            kind="delivery_manifest",
            path=str(path),
            summary=f"Delivery manifest for task {manifest.get('task_id')}",
            lifecycle="persistent",
        )
        return str(path), artifact

    def delivery_package(
        self,
        contract: DeliveryContract,
        manifest: ArtifactManifest,
        gate: QualityGate,
        report_paths: list[str],
        artifacts: list[ArtifactRecord],
        *,
        status: str,
    ) -> DeliveryPackage:
        title = compact((self.state.get("current_intent") or {}).get("goal") or (self.state.get("db_task_plan") or {}).get("summary") or "Database task delivery", 120)
        summary = f"Delivery package {status}; reports={len(report_paths)} artifacts={len(artifacts)} missing={len(manifest.get('missing_items', []))}."
        return {
            "id": new_id("delivery-package"),
            "task_id": manifest["task_id"],
            "contract_id": contract["id"],
            "title": title,
            "status": status,  # type: ignore[typeddict-item]
            "summary": summary,
            "user_report_path": report_paths[0] if report_paths else None,
            "audit_report_path": report_paths[1] if len(report_paths) > 1 else None,
            "manifest_id": manifest["id"],
            "artifact_ids": [artifact["id"] for artifact in artifacts],
            "quality_gate_ids": [gate["id"]],
            "next_actions": self.next_actions(contract, manifest, manifest.get("missing_items", [])),
            "created_at": now_iso(),
            "delivered_at": now_iso() if status == "ready" else None,
        }

    def render_user_report(self, contract: DeliveryContract, manifest: ArtifactManifest, sections: list[ReportSection]) -> str:
        lines = [
            "# PostgreSQL Task Delivery Report",
            "",
            f"- Delivery status: {contract.get('status')}",
            f"- Task: {manifest.get('task_id')}",
            f"- Evidence refs: {len(manifest.get('evidence_refs', []))}",
            f"- SQL items: {len(manifest.get('sql_items', []))}",
            "",
        ]
        for section in sections:
            if section["purpose"] in {"summary", "diagnosis", "evidence", "risk", "verification", "next_steps"}:
                lines.extend([f"## {section['title']}", "", section["content"] or "No content.", ""])
        return "\n".join(lines).strip() + "\n"

    def render_audit_report(self, contract: DeliveryContract, manifest: ArtifactManifest, sections: list[ReportSection]) -> str:
        payload = {
            "contract": contract,
            "manifest": manifest,
            "sections": sections,
            "quality_gates": self.state.get("quality_gates", [])[-10:],
            "model_invocations": self.state.get("model_invocation_records", [])[-10:],
            "tool_invocations": self.state.get("tool_invocation_records", [])[-10:],
        }
        return "# PostgreSQL Task Audit Report\n\n```json\n" + json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n```\n"

    def next_actions(self, contract: DeliveryContract, manifest: ArtifactManifest, missing: list[str]) -> list[str]:
        if missing:
            actions = ["request_more_evidence"]
        else:
            actions = ["review_final_report"]
        if contract.get("requires_approval_evidence") and not any(item.get("approval_id") for item in manifest.get("sql_items", [])):
            actions.append("approve_execution")
        if contract.get("requires_verification") and not self.state.get("verification_results"):
            actions.append("run_verification")
        if self.state.get("loop_status") == "blocked" or self.state.get("error_reports"):
            actions.extend(["report_only", "adjust_task_scope"])
        actions.append("export_audit_package")
        return list(dict.fromkeys(actions))

    def _section(self, purpose: str, title: str, content: str, evidence_refs: list[str], status: str) -> ReportSection:
        return {
            "id": new_id("report-section"),
            "title": title,
            "purpose": purpose,  # type: ignore[typeddict-item]
            "content": content,
            "evidence_refs": evidence_refs,
            "status": status,  # type: ignore[typeddict-item]
        }

    def _evidence(self, source_type: str, source_id: Any, summary: Any, supports: str, sensitivity: Any) -> EvidenceReference:
        return {
            "id": new_id("evidence"),
            "source_type": source_type,  # type: ignore[typeddict-item]
            "source_id": str(source_id or ""),
            "summary": compact(summary, 300),
            "supports_claim": compact(supports, 200),
            "sensitivity": str(sensitivity or "internal"),  # type: ignore[typeddict-item]
        }

    def _task_id(self) -> str:
        return str((self.state.get("db_task_plan") or {}).get("id") or (self.state.get("current_intent") or {}).get("id") or "task-unknown")

    def _task_workspace(self) -> dict[str, Any]:
        return self.state.get("task_workspace") or {}

    def _target_environment(self) -> str:
        return str((self.state.get("database_environment") or {}).get("environment_name") or (self.state.get("current_intent") or {}).get("target_environment") or "unknown")

    def _state_sensitivity(self) -> str:
        values = [artifact.get("sensitivity") for artifact in self.state.get("artifact_records", [])]
        values.extend(ref.get("sensitivity") for ref in self.state.get("evidence_references", []))
        max_value = max((sensitivity_rank(value) for value in values), default=2)
        return {1: "public", 2: "internal", 3: "sensitive", 4: "secret"}[max_value]

    def _missing_manifest_items(self, evidence_refs: list[EvidenceReference], sql_items: list[SQLDeliveryItem]) -> list[str]:
        contract = self.state.get("active_delivery_contract") or self.build_contract()
        missing: list[str] = []
        if contract.get("required_evidence_types") and not evidence_refs:
            missing.append("required_evidence")
        if contract.get("requires_sql_package") and not sql_items:
            missing.append("sql_change_package")
        if contract.get("requires_approval_evidence") and not any(item.get("approval_id") for item in sql_items):
            missing.append("approval_package")
        if contract.get("requires_verification") and not self.state.get("verification_results"):
            missing.append("verification_report")
        return list(dict.fromkeys(missing))

    def _approval_for_sql(self, sql_hash: str, approvals: list[dict[str, Any]]) -> dict[str, Any] | None:
        return next((approval for approval in approvals if approval.get("sql_hash") == sql_hash), None)

    def _execution_record_for_sql(self, sql_hash: str) -> str | None:
        for record in self.state.get("tool_invocation_records", []):
            digest = record.get("args_digest") or {}
            if digest.get("sql_hash") == sql_hash:
                return record.get("id")
        return None

    @staticmethod
    def _sql_purpose(classification: str, sql: str) -> str:
        lowered = sql.lower()
        if "explain" in lowered or classification == "read_only":
            return "diagnostic"
        if "rollback" in lowered:
            return "rollback"
        if classification in WRITE_CLASSIFICATIONS:
            return "change"
        return "verification" if "select" in lowered else "dry_run"

    @staticmethod
    def _sql_status(classification: str, approval: dict[str, Any] | None, verification_ids: list[str]) -> str:
        if verification_ids:
            return "verified"
        if approval and approval.get("status") == "approved":
            return "approved"
        if classification in WRITE_CLASSIFICATIONS and not approval:
            return "blocked"
        return "draft"

    def _evidence_section(self, manifest: ArtifactManifest) -> str:
        if not manifest["evidence_refs"]:
            return "No structured evidence is available."
        return "\n".join(
            f"- [{ref['source_type']}:{ref['source_id']}] {ref['summary']}"
            for ref in manifest["evidence_refs"][:12]
        )

    def _risk_section(self, manifest: ArtifactManifest) -> str:
        runtime = self.state.get("db_task_runtime") or {}
        lines = [
            f"- Target environment: {self._target_environment()}",
            f"- Risk level: {runtime.get('risk_level') or (self.state.get('db_task_plan') or {}).get('global_risk_level') or 'unknown'}",
            f"- Missing delivery items: {', '.join(manifest.get('missing_items', [])) or 'none'}",
        ]
        if self.state.get("policy_violation"):
            lines.append(f"- Policy violation: {compact((self.state.get('policy_violation') or {}).get('message'), 240)}")
        return "\n".join(lines)

    def _verification_section(self) -> str:
        verifications = self.state.get("verification_results", [])
        if not verifications:
            return "No verification result is available yet."
        return "\n".join(
            f"- {item.get('step_id')}: {item.get('status')} - {compact(item.get('summary'), 240)}"
            for item in verifications[-8:]
        )

    @staticmethod
    def _sql_section(items: list[SQLDeliveryItem]) -> str:
        if not items:
            return "No SQL delivery item is available."
        return "\n".join(
            f"- {item['purpose']} {item['status']} risk={item['risk_level']} hash={item['sql_hash'][:12]} approval={item.get('approval_id') or 'none'}"
            for item in items
        )

    def _blocked_section(self) -> str:
        reports = self.state.get("error_reports", [])
        if reports:
            return "\n".join(f"- {compact(item.get('user_summary'), 300)}" for item in reports[-3:])
        violation = self.state.get("policy_violation") or {}
        if violation:
            return f"- {compact(violation.get('message'), 400)}"
        return "- Task is blocked or not fully completed."

    @staticmethod
    def _raw_secret_in_reports(paths: list[str]) -> bool:
        for path in paths:
            try:
                text = Path(path).read_text(encoding="utf-8")[:20000]
            except OSError:
                continue
            if SENSITIVE_RE.search(text):
                return True
        return False
