"""Quality gates, task evaluation, replay cases, and reports."""

from __future__ import annotations

import operator
import re
import uuid
from datetime import datetime, timezone
from functools import reduce
from typing import Any

from agent.state import (
    AgentState,
    EvaluationCase,
    EvaluationResult,
    QualityGate,
    QualityReport,
    ReplayCase,
    RegisteredToolSpec,
)


HIGH_RISK_PATH_PATTERNS = (
    "safety/",
    "tools/builtin/postgres.py",
    "tools/postgres/",
    "agent/nodes/error_handler.py",
    "error_handling/",
    "state_management/",
    "memory/",
    "agent/nodes/agent_loop.py",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _compact(value: Any, limit: int = 500) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _status(required: list[str], passed: list[str], failed: list[str]) -> str:
    if failed:
        return "failed"
    if required and len(passed) >= len(required):
        return "passed"
    return "pending"


def _value_at_path(data: Any, path: str) -> Any:
    current = data
    for part in path.split("."):
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list):
            try:
                current = current[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return current


def _assertion_passed(state: AgentState, assertion: dict[str, Any]) -> bool:
    path = str(assertion.get("path") or "")
    op = str(assertion.get("op") or "equals")
    expected = assertion.get("value")
    actual = _value_at_path(state, path)
    if op == "equals":
        return actual == expected
    if op == "not_equals":
        return actual != expected
    if op == "contains":
        return expected in (actual or [])
    if op == "exists":
        return actual is not None
    if op == "truthy":
        return bool(actual)
    if op == "count_at_least":
        return len(actual or []) >= int(expected)
    return False


class QualityManager:
    """Builders for quality gates, evaluation results, replay cases, and reports."""

    def __init__(self, state: AgentState | None = None) -> None:
        self.state = state or {}

    @staticmethod
    def gate(
        *,
        gate_type: str,
        target_ref: str,
        required_checks: list[str],
        passed_checks: list[str] | None = None,
        failed_checks: list[str] | None = None,
        blocking: bool = True,
    ) -> QualityGate:
        passed = passed_checks or []
        failed = failed_checks or []
        return {
            "id": _new_id("quality-gate"),
            "gate_type": gate_type,  # type: ignore[typeddict-item]
            "target_ref": target_ref,
            "required_checks": required_checks,
            "passed_checks": passed,
            "failed_checks": failed,
            "status": _status(required_checks, passed, failed),  # type: ignore[typeddict-item]
            "blocking": blocking,
            "created_at": _now_iso(),
        }

    def task_completion_gate(self) -> QualityGate:
        plan = self.state.get("db_task_plan") or {}
        intent = self.state.get("current_intent") or {}
        target_ref = str(plan.get("id") or intent.get("id") or "current-task")
        steps = list(plan.get("steps", []) or self.state.get("task_stack", []) or [])
        has_write = any(step.get("operation_type") in {"schema_change", "data_change", "permission_change", "backup_restore", "maintenance"} for step in steps)
        has_report = any(step.get("phase") == "report" for step in steps)
        required = ["plan_exists", "steps_verified", "state_integrity_ok"]
        if intent.get("domain") == "postgresql":
            required.append("evidence_available")
        if has_write:
            required.extend(["approval_recorded", "sql_hash_recorded", "rollback_documented", "verification_recorded"])
        if has_report or intent.get("primary_intent") == "documentation":
            required.append("report_has_evidence")

        passed: list[str] = []
        failed: list[str] = []
        if plan or steps:
            passed.append("plan_exists")
        else:
            failed.append("plan_exists")
        if steps and all(step.get("status") in {"completed", "skipped"} for step in steps):
            passed.append("steps_verified")
        else:
            failed.append("steps_verified")
        latest_integrity = (self.state.get("state_integrity_reports") or [{}])[-1]
        if not latest_integrity or latest_integrity.get("ok", True):
            passed.append("state_integrity_ok")
        else:
            failed.append("state_integrity_ok")
        if "evidence_available" in required:
            (passed if self.state.get("db_observations") else failed).append("evidence_available")
        if "approval_recorded" in required:
            approvals = self.state.get("approval_decisions", [])
            (passed if approvals else failed).append("approval_recorded")
            (passed if any(item.get("sql_hash") for item in approvals) else failed).append("sql_hash_recorded")
            (passed if any(item.get("rollback_summary") for item in approvals) else failed).append("rollback_documented")
            (passed if self.state.get("verification_results") else failed).append("verification_recorded")
        if "report_has_evidence" in required:
            artifacts = self.state.get("artifact_records", [])
            (passed if any(item.get("kind") == "final_report" for item in artifacts) and self.state.get("db_observations") else failed).append("report_has_evidence")
        return self.gate(
            gate_type="task_completion",
            target_ref=target_ref,
            required_checks=required,
            passed_checks=list(dict.fromkeys(passed)),
            failed_checks=list(dict.fromkeys(failed)),
            blocking=True,
        )

    def tool_contract_gate(self, spec: RegisteredToolSpec) -> QualityGate:
        capability = spec.get("capability") or {}
        required = [
            "args_schema_declared",
            "capability_declared",
            "allowed_phases_declared",
            "allowed_policies_declared",
            "output_type_declared",
            "sensitivity_declared",
            "replay_policy_derivable",
        ]
        passed: list[str] = []
        failed: list[str] = []
        checks = {
            "args_schema_declared": bool(spec.get("args_schema")),
            "capability_declared": bool(capability.get("domain") and capability.get("operation_type")),
            "allowed_phases_declared": bool(spec.get("allowed_phases")),
            "allowed_policies_declared": bool(spec.get("allowed_policies")),
            "output_type_declared": bool(spec.get("output_type")),
            "sensitivity_declared": bool(spec.get("result_sensitivity")),
            "replay_policy_derivable": bool(spec.get("name")),
        }
        for name, ok in checks.items():
            (passed if ok else failed).append(name)
        return self.gate(
            gate_type="tool_contract",
            target_ref=str(spec.get("name") or "tool"),
            required_checks=required,
            passed_checks=passed,
            failed_checks=failed,
            blocking=True,
        )

    def safety_regression_gate(self) -> QualityGate:
        required = [
            "no_unknown_environment_writes",
            "no_production_writes",
            "write_sql_has_approval",
            "approval_has_sql_hash",
            "no_policy_violation",
        ]
        passed: list[str] = []
        failed: list[str] = []
        db_env = self.state.get("database_environment") or {}
        runtime_policy = self.state.get("runtime_policy") or {}
        approvals = self.state.get("approval_decisions", [])
        sql_reports = self.state.get("sql_safety_reports", [])
        has_write_sql = any(item.get("classification") in {"data_change", "schema_change", "permission_change", "maintenance", "transaction_control"} for item in sql_reports)

        (failed if db_env.get("environment_name") == "unknown" and runtime_policy.get("allow_database_writes") else passed).append("no_unknown_environment_writes")
        (failed if db_env.get("is_production") and runtime_policy.get("allow_database_writes") else passed).append("no_production_writes")
        (passed if not has_write_sql or approvals else failed).append("write_sql_has_approval")
        (passed if not approvals or all(item.get("sql_hash") for item in approvals if item.get("sql_preview")) else failed).append("approval_has_sql_hash")
        (failed if self.state.get("policy_violation") else passed).append("no_policy_violation")
        return self.gate(
            gate_type="safety_regression",
            target_ref=str((self.state.get("db_task_plan") or {}).get("id") or "current-state"),
            required_checks=required,
            passed_checks=list(dict.fromkeys(passed)),
            failed_checks=list(dict.fromkeys(failed)),
            blocking=True,
        )

    def state_integrity_gate(self) -> QualityGate:
        latest = (self.state.get("state_integrity_reports") or [{}])[-1]
        failed = list(latest.get("errors", []) or [])
        warnings = list(latest.get("warnings", []) or [])
        gate = self.gate(
            gate_type="state_integrity",
            target_ref=str((self.state.get("state_metadata") or {}).get("session_id") or self.state.get("session_id") or "state"),
            required_checks=["state_integrity_ok"],
            passed_checks=["state_integrity_ok"] if not failed else [],
            failed_checks=failed,
            blocking=bool(failed),
        )
        gate["failed_checks"] = failed + [f"warning:{item}" for item in warnings]
        return gate

    def error_recovery_gate(self) -> QualityGate:
        required = ["errors_classified", "recovery_decision_present", "retry_budget_recorded"]
        errors = self.state.get("error_records", [])
        decisions = self.state.get("recovery_decisions", [])
        budgets = self.state.get("retry_budgets", [])
        passed: list[str] = []
        failed: list[str] = []
        (passed if errors else failed).append("errors_classified")
        (passed if decisions else failed).append("recovery_decision_present")
        (passed if budgets else failed).append("retry_budget_recorded")
        return self.gate(
            gate_type="error_recovery",
            target_ref=str((errors[-1] or {}).get("id") if errors else "error-recovery"),
            required_checks=required,
            passed_checks=passed,
            failed_checks=failed,
            blocking=bool(errors and not decisions),
        )

    def run_evaluation_case(self, case: EvaluationCase, state: AgentState | None = None) -> EvaluationResult:
        state = state or self.state
        failed: list[str] = []
        for assertion in case.get("expected_state_assertions", []):
            if not _assertion_passed(state, assertion):
                failed.append(f"state:{assertion}")
        output_text = " ".join(str(getattr(msg, "content", msg.get("content", "")) if isinstance(msg, dict) else getattr(msg, "content", "")) for msg in state.get("messages", []))
        for assertion in case.get("expected_output_assertions", []):
            value = str(assertion.get("value") or "")
            if assertion.get("op", "contains") == "contains" and value not in output_text:
                failed.append(f"output:{assertion}")
        tool_names = {item.get("tool_name") for item in state.get("tool_invocation_records", [])}
        for forbidden in case.get("forbidden_actions", []):
            if forbidden in tool_names:
                failed.append(f"forbidden_action:{forbidden}")
        required_evidence = case.get("required_evidence", [])
        evidence_refs = [
            *[item.get("id") for item in state.get("db_observations", []) if item.get("id")],
            *[item.get("id") for item in state.get("artifact_records", []) if item.get("id")],
        ]
        if required_evidence and not evidence_refs:
            failed.append("required_evidence_missing")

        safety_blocked = bool(state.get("policy_violation")) or any(
            decision.get("decision") == "deny"
            for decision in state.get("security_policy_decisions", [])
        )
        scores = {
            "state_assertions": 1.0 if not any(item.startswith("state:") for item in failed) else 0.0,
            "output_assertions": 1.0 if not any(item.startswith("output:") for item in failed) else 0.0,
            "tool_compliance": 1.0 if not any(item.startswith("forbidden_action:") for item in failed) else 0.0,
            "evidence": 1.0 if "required_evidence_missing" not in failed else 0.0,
            "safety": 1.0 if not safety_blocked or case.get("category") == "safety" else 0.0,
        }
        status = "failed" if failed else ("needs_review" if safety_blocked and case.get("category") != "safety" else "passed")
        return {
            "id": _new_id("eval-result"),
            "case_id": case["id"],
            "status": status,  # type: ignore[typeddict-item]
            "scores": scores,
            "failed_assertions": failed,
            "evidence_refs": [str(item) for item in evidence_refs],
            "safety_blocked": safety_blocked,
            "requires_human_review": status == "needs_review",
            "summary": "Evaluation passed." if not failed else f"Evaluation failed: {', '.join(failed[:3])}",
            "created_at": _now_iso(),
        }

    def replay_case_from_state(
        self,
        *,
        source: str = "manual",
        expected_recovery: str | None = None,
        expected_final_status: str | None = None,
        sensitivity: str = "internal",
    ) -> ReplayCase:
        messages = []
        for msg in self.state.get("messages", []):
            messages.append(
                {
                    "type": getattr(msg, "type", None) or (msg.get("type") if isinstance(msg, dict) else type(msg).__name__),
                    "content": _compact(getattr(msg, "content", None) or (msg.get("content") if isinstance(msg, dict) else ""), 1000),
                }
            )
        return {
            "id": _new_id("replay"),
            "source": source,  # type: ignore[typeddict-item]
            "input_messages": messages,
            "state_snapshot_ref": "state.context_snapshots[-1]" if self.state.get("context_snapshots") else None,
            "tool_invocation_refs": [str(item.get("id")) for item in self.state.get("tool_invocation_records", []) if item.get("id")],
            "expected_recovery": expected_recovery,
            "expected_final_status": expected_final_status or str((self.state.get("db_task_runtime") or {}).get("task_status") or self.state.get("loop_status") or "unknown"),
            "sensitivity": sensitivity,  # type: ignore[typeddict-item]
            "created_at": _now_iso(),
        }

    def high_risk_change_gate(self, changed_paths: list[str]) -> QualityGate:
        risky = [
            path for path in changed_paths
            if any(path.startswith(pattern) or pattern in path for pattern in HIGH_RISK_PATH_PATTERNS)
        ]
        required = ["high_risk_files_identified", "security_tests_required", "human_review_required"]
        if risky:
            return self.gate(
                gate_type="human_review",
                target_ref="code-change",
                required_checks=required,
                passed_checks=["high_risk_files_identified"],
                failed_checks=["security_tests_required", "human_review_required"],
                blocking=True,
            )
        return self.gate(
            gate_type="human_review",
            target_ref="code-change",
            required_checks=["no_high_risk_files"],
            passed_checks=["no_high_risk_files"],
            failed_checks=[],
            blocking=False,
        )

    def quality_report(
        self,
        *,
        target_ref: str,
        scope: str,
        gates: list[QualityGate] | None = None,
        evaluation_results: list[EvaluationResult] | None = None,
        recommendations: list[str] | None = None,
    ) -> QualityReport:
        gates = gates if gates is not None else self.state.get("quality_gates", [])
        evaluation_results = evaluation_results if evaluation_results is not None else self.state.get("evaluation_results", [])
        failed_gates = [gate for gate in gates if gate.get("status") == "failed"]
        review_gates = [gate for gate in gates if gate.get("status") == "pending" and gate.get("blocking")]
        failed_evals = [item for item in evaluation_results if item.get("status") == "failed"]
        needs_review_evals = [item for item in evaluation_results if item.get("requires_human_review")]
        safety_gates = [gate for gate in gates if gate.get("gate_type") == "safety_regression"]
        human_review_required = bool(review_gates or needs_review_evals or any(gate.get("gate_type") == "human_review" and gate.get("blocking") and gate.get("status") != "passed" for gate in gates))
        status = "failed" if failed_gates or failed_evals else ("needs_review" if human_review_required else "passed")
        uncovered_risks = []
        if not safety_gates:
            uncovered_risks.append("safety_regression_not_run")
        if not evaluation_results:
            uncovered_risks.append("evaluation_cases_not_run")
        return {
            "id": _new_id("quality-report"),
            "target_ref": target_ref,
            "scope": scope,  # type: ignore[typeddict-item]
            "status": status,  # type: ignore[typeddict-item]
            "test_summary": {
                "quality_gates": len(gates),
                "failed_gates": len(failed_gates),
                "blocking_gates": len([gate for gate in gates if gate.get("blocking")]),
            },
            "evaluation_summary": {
                "evaluation_results": len(evaluation_results),
                "failed_results": len(failed_evals),
                "needs_review": len(needs_review_evals),
                "average_score": self._average_score(evaluation_results),
            },
            "safety_summary": {
                "safety_gates": len(safety_gates),
                "safety_failed": any(gate.get("status") == "failed" for gate in safety_gates),
                "policy_violation": bool(self.state.get("policy_violation")),
            },
            "uncovered_risks": uncovered_risks,
            "human_review_required": human_review_required,
            "recommendations": recommendations or self._recommendations(status, uncovered_risks),
            "created_at": _now_iso(),
        }

    @staticmethod
    def _average_score(results: list[EvaluationResult]) -> float:
        values = [
            score
            for result in results
            for score in (result.get("scores") or {}).values()
        ]
        if not values:
            return 0.0
        return round(reduce(operator.add, values, 0.0) / len(values), 3)

    @staticmethod
    def _recommendations(status: str, uncovered_risks: list[str]) -> list[str]:
        if status == "failed":
            return ["Fix failed quality gates before continuing.", "Rerun safety regression and affected evaluation cases."]
        if status == "needs_review":
            return ["Request human review for high-risk or ambiguous quality results."]
        if uncovered_risks:
            return ["Run missing quality checks before release."]
        return ["Quality checks passed for the selected scope."]
