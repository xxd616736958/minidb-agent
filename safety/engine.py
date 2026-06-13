"""Central safety guardrail policy engine.

The engine is intentionally independent from LangGraph nodes. Nodes and tools
call it to turn state, tool metadata, SQL text, approvals, and memory into
structured allow/deny/approval decisions.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Any

from agent.context import build_step_context_packet, retrieve_relevant_memories
from agent.state import (
    AgentState,
    ApprovalBinding,
    ApprovalDecision,
    OutputSafetyPolicy,
    RegisteredToolSpec,
    SQLSafetyReport,
    SafetyAuditRecord,
    SecurityPolicyDecision,
    TaskStep,
    ToolCallPolicyDecision,
)
from tools.postgres.sql_safety import classify_sql, normalize_sql


POSTGRES_MUTATING_OPERATION_TYPES = {
    "data_change",
    "schema_change",
    "permission_change",
    "backup_restore",
    "maintenance",
}
POSTGRES_DIAGNOSTIC_TOOLS_ALLOW_WRITE_SQL_INPUT = {
    "postgres_dry_run",
    "postgres_sql_classify",
}
SQL_ARG_KEYS = ("sql", "query", "statement")
RISK_ORDER = {"low": 1, "medium": 2, "high": 3, "critical": 4}
SENSITIVE_FIELD_PATTERNS = [
    "password",
    "passwd",
    "pwd",
    "secret",
    "token",
    "api[_-]?key",
    "credential",
    "private[_-]?key",
    "email",
    "phone",
    "id_card",
]
DEFAULT_OUTPUT_SAFETY_POLICY: OutputSafetyPolicy = {
    "max_rows": 100,
    "max_chars": 20_000,
    "mask_sensitive_fields": True,
    "sensitive_field_patterns": SENSITIVE_FIELD_PATTERNS,
    "allow_raw_result_in_context": False,
    "allow_raw_result_in_memory": False,
    "artifact_required_for_large_output": True,
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _risk_max(left: str, right: str) -> str:
    return left if RISK_ORDER.get(left, 0) >= RISK_ORDER.get(right, 0) else right


def _sql_from_args(args: Any) -> str | None:
    if not isinstance(args, dict):
        return None
    for key in SQL_ARG_KEYS:
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _quote_ident(value: Any) -> str:
    text = str(value or "").strip()
    if not text or not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", text):
        raise ValueError(f"Unsafe PostgreSQL identifier: {text!r}")
    return text


def _quote_qualified_name(value: Any) -> str:
    text = str(value or "").strip()
    parts = text.split(".")
    if not parts or len(parts) > 2:
        raise ValueError(f"Unsafe PostgreSQL qualified name: {text!r}")
    return ".".join(_quote_ident(part) for part in parts)


def _project_sql_from_tool_call(tool_name: str, args: Any) -> str | None:
    """Return auditable SQL for parameterized PostgreSQL write tools."""
    if not isinstance(args, dict):
        return None
    if tool_name == "postgres_create_index_concurrently":
        table = _quote_qualified_name(args.get("table_name"))
        columns = args.get("columns") or []
        if not isinstance(columns, list) or not columns:
            return None
        column_sql = ", ".join(_quote_ident(column) for column in columns)
        using = str(args.get("using") or "btree").lower()
        if using not in {"btree", "hash", "gist", "gin", "brin", "spgist"}:
            return None
        index_name = args.get("index_name")
        index_sql = f"IF NOT EXISTS {_quote_ident(index_name)} " if index_name else ""
        return f"CREATE INDEX CONCURRENTLY {index_sql}ON {table} USING {using} ({column_sql});"
    return None


def _current_step(state: AgentState) -> TaskStep | None:
    steps = list(state.get("task_stack", []))
    step_id = state.get("current_step_id")
    if step_id:
        for step in steps:
            if step.get("id") == step_id:
                return step
    idx = int(state.get("current_task_index", 0) or 0)
    if steps and idx < len(steps):
        return steps[idx]
    return None


def _is_postgres_tool_name(tool_name: str) -> bool:
    lowered = tool_name.lower()
    return any(token in lowered for token in ("postgres", "postgresql", "sql", "database", "db"))


def _extract_target_objects(sql: str) -> list[dict[str, Any]]:
    targets: list[dict[str, Any]] = []
    patterns = [
        ("table", r"\bfrom\s+([a-zA-Z_][\w\.]*)"),
        ("table", r"\bjoin\s+([a-zA-Z_][\w\.]*)"),
        ("table", r"\bupdate\s+([a-zA-Z_][\w\.]*)"),
        ("table", r"\binto\s+([a-zA-Z_][\w\.]*)"),
        ("table", r"\btable\s+([a-zA-Z_][\w\.]*)"),
        ("index", r"\bindex(?:\s+concurrently)?\s+([a-zA-Z_][\w\.]*)"),
        ("schema", r"\bschema\s+([a-zA-Z_][\w\.]*)"),
    ]
    seen: set[tuple[str, str]] = set()
    for obj_type, pattern in patterns:
        for match in re.finditer(pattern, sql, re.IGNORECASE):
            name = match.group(1)
            key = (obj_type, name)
            if key in seen:
                continue
            seen.add(key)
            targets.append({"type": obj_type, "name": name})
    return targets


def build_sql_safety_report(sql: str, *, allow_explain_analyze: bool = False) -> SQLSafetyReport:
    classification = classify_sql(sql, allow_explain_analyze=allow_explain_analyze)
    primary = classification.primary_type
    if primary == "explain":
        primary = "diagnostic"
    if primary not in {
        "read_only",
        "diagnostic",
        "data_change",
        "schema_change",
        "permission_change",
        "maintenance",
        "transaction_control",
    }:
        primary = "unknown"

    dangerous = [*classification.blocked_reasons]
    for warning in classification.warnings:
        if "without WHERE" in warning or "multiple SQL statements" in warning:
            dangerous.append(warning)

    denial_reason = None
    if classification.blocked_reasons:
        denial_reason = "; ".join(classification.blocked_reasons)
    elif primary == "unknown":
        denial_reason = "SQL classification is unknown."

    return {
        "sql_hash": classification.normalized_sql_hash,
        "normalized_sql_preview": normalize_sql(sql)[:500],
        "classification": primary,  # type: ignore[typeddict-item]
        "contains_multiple_statements": classification.statement_count > 1,
        "contains_dangerous_constructs": sorted(set(dangerous)),
        "target_objects": _extract_target_objects(sql),
        "requires_approval": classification.requires_approval,
        "requires_rollback_plan": classification.destructive,
        "requires_backup_check": primary in {"data_change", "schema_change", "permission_change", "maintenance"},
        "can_run_in_readonly_transaction": bool(classification.read_only),
        "risk_level": classification.risk_level,  # type: ignore[typeddict-item]
        "denial_reason": denial_reason,
    }


def build_approval_binding(
    approval: ApprovalDecision,
    *,
    tool_name: str,
    target_database: str | None = None,
) -> ApprovalBinding:
    return {
        "approval_id": approval["id"],
        "step_id": approval["step_id"],
        "tool_name": tool_name,
        "target_environment": approval["target_environment"],
        "target_database": target_database,
        "sql_hash": approval.get("sql_hash"),
        "impact_summary": approval.get("impact_summary") or "",
        "rollback_summary": approval.get("rollback_summary") or "",
        "verification_criteria": list(approval.get("verification_criteria", [])),
        "expires_at": None,
    }


def safety_decision_to_tool_decision(
    decision: SecurityPolicyDecision,
    *,
    call_id: str,
    tool_name: str,
) -> ToolCallPolicyDecision:
    reason = "; ".join(decision["reasons"]) or decision["decision"]
    return {
        "call_id": call_id,
        "tool_name": tool_name,
        "decision": decision["decision"],
        "reason": reason,
        "risk_level": decision["risk_level"],
        "approval_required": decision["decision"] == "require_approval",
        "approval_payload": decision["approval_payload"],
    }


class SecurityPolicyEngine:
    """Evaluate tool, SQL, approval, environment, memory, and output policies."""

    def __init__(self, state: AgentState | None = None) -> None:
        self.state = state or {}

    def default_output_policy(self) -> OutputSafetyPolicy:
        db_env = self.state.get("database_environment") or {}
        policy = dict(DEFAULT_OUTPUT_SAFETY_POLICY)
        if db_env.get("max_result_rows"):
            policy["max_rows"] = int(db_env["max_result_rows"])
        return policy  # type: ignore[return-value]

    def _decision(
        self,
        *,
        scope: str,
        subject: str,
        decision: str,
        risk_level: str = "low",
        reasons: list[str] | None = None,
        matched_rules: list[str] | None = None,
        approval_payload: dict[str, Any] | None = None,
    ) -> SecurityPolicyDecision:
        return {
            "id": new_id("sec"),
            "scope": scope,  # type: ignore[typeddict-item]
            "subject": subject,
            "decision": decision,  # type: ignore[typeddict-item]
            "risk_level": risk_level if risk_level in RISK_ORDER else "high",  # type: ignore[typeddict-item]
            "reasons": reasons or [],
            "matched_rules": matched_rules or [],
            "approval_payload": approval_payload,
            "created_at": now_iso(),
        }

    def audit_for_decision(
        self,
        decision: SecurityPolicyDecision,
        *,
        step_id: str | None = None,
        tool_name: str | None = None,
    ) -> SafetyAuditRecord:
        decision_value = decision["decision"]
        scope = decision["scope"]
        if scope == "tool_visibility":
            event_type = "tool_visible" if decision_value == "allow" else "tool_hidden"
        elif scope == "tool_call":
            event_type = "tool_allowed" if decision_value == "allow" else (
                "approval_requested" if decision_value == "require_approval" else "tool_denied"
            )
        elif scope == "sql_execution":
            event_type = "sql_allowed" if decision_value == "allow" else (
                "approval_requested" if decision_value == "require_approval" else "sql_denied"
            )
        elif scope == "output_handling":
            event_type = "output_masked"
        elif scope == "state_replay":
            event_type = "replay_blocked"
        else:
            event_type = "tool_denied"
        return {
            "id": new_id("audit"),
            "event_type": event_type,  # type: ignore[typeddict-item]
            "step_id": step_id,
            "tool_name": tool_name,
            "decision_id": decision["id"],
            "summary": "; ".join(decision["reasons"]) or decision["decision"],
            "created_at": now_iso(),
        }

    def _current_step_and_policy(self) -> tuple[TaskStep | None, str]:
        step = _current_step(self.state)
        packet = self.state.get("step_context")
        if step and packet and packet.get("step_id") != step.get("id"):
            packet = None
        packet = packet or build_step_context_packet(self.state)
        policy = str((packet or {}).get("tool_policy") or (step or {}).get("tool_policy", "no_tools"))
        return step, policy

    def _is_mutating_postgres_spec(self, spec: RegisteredToolSpec | None) -> bool:
        if not spec:
            return False
        capability = spec["capability"]
        return (
            capability["domain"] == "postgresql"
            and (capability["destructive"] or capability["operation_type"] in POSTGRES_MUTATING_OPERATION_TYPES)
        )

    def _safety_memory_violation(self, tool_call: dict[str, Any], spec: RegisteredToolSpec | None) -> str | None:
        if not self._is_write_intent(tool_call, spec):
            return None

        memories = self.state.get("retrieved_memories")
        if memories is None:
            memories = retrieve_relevant_memories(self.state, limit=8)

        call_text = f"{tool_call.get('name', '')} {tool_call.get('args', {})}".lower()
        for memory in memories:
            if memory.get("kind") not in {"prohibition", "preference"}:
                continue
            text = f"{memory.get('summary', '')} {memory.get('payload', {})}".lower()
            hard_rule = memory.get("kind") == "prohibition"
            if hard_rule and (
                "只读" in text
                or "read-only" in text
                or "read only" in text
                or "不要执行" in text
                or "禁止" in text
            ):
                return f"Long-term SafetyMemory prohibits write SQL: {memory.get('summary')}"
            blocked_verbs = ("drop", "truncate", "delete", "update", "insert", "alter", "grant", "revoke")
            if hard_rule and any(verb in text and re.search(rf"\b{verb}\b", call_text) for verb in blocked_verbs):
                return f"Long-term SafetyMemory blocks this SQL pattern: {memory.get('summary')}"
        return None

    def _is_write_intent(self, tool_call: dict[str, Any], spec: RegisteredToolSpec | None) -> bool:
        tool_name = str(tool_call.get("name", ""))
        if tool_name in POSTGRES_DIAGNOSTIC_TOOLS_ALLOW_WRITE_SQL_INPUT:
            return False
        if self._is_mutating_postgres_spec(spec):
            return True
        sql = _sql_from_args(tool_call.get("args"))
        if sql and _is_postgres_tool_name(tool_name):
            report = build_sql_safety_report(sql, allow_explain_analyze=True)
            return report["classification"] in {
                "data_change",
                "schema_change",
                "permission_change",
                "maintenance",
                "transaction_control",
            }
        args_text = f"{tool_call.get('args', {})}".lower()
        if tool_name == "shell_execute" and re.search(r"\b(psql|pg_restore|dropdb|createdb|vacuumdb|reindexdb)\b", args_text):
            return bool(
                re.search(
                    r"\b(insert|update|delete|merge|alter|drop|truncate|create|grant|revoke|vacuum|reindex)\b",
                    args_text,
                )
            )
        return False

    def _approved_binding(
        self,
        *,
        step: TaskStep | None,
        tool_name: str,
        sql_report: SQLSafetyReport | None,
    ) -> ApprovalBinding | None:
        if not step:
            return None
        db_env = self.state.get("database_environment") or {}
        target_environment = str(db_env.get("environment_name") or "unknown")
        target_database = db_env.get("target_database")
        allowed_step_ids = {str(step.get("id") or "")}
        allowed_step_ids.update(str(item) for item in step.get("dependencies", []) if item)
        for approval in reversed(self.state.get("approval_decisions", [])):
            if approval.get("status") != "approved":
                continue
            if str(approval.get("step_id") or "") not in allowed_step_ids:
                continue
            if approval.get("target_environment") not in {target_environment, "unknown", None}:
                continue
            approved_hash = approval.get("sql_hash")
            if sql_report and approved_hash and approved_hash != sql_report["sql_hash"]:
                continue
            if sql_report and not approved_hash:
                continue
            return build_approval_binding(approval, tool_name=tool_name, target_database=target_database)
        return None

    def evaluate_tool_visibility(self, spec: RegisteredToolSpec) -> SecurityPolicyDecision:
        step, policy = self._current_step_and_policy()
        capability = spec["capability"]
        risk = capability.get("risk_level", "low")
        db_env = self.state.get("database_environment") or {}
        runtime_policy = self.state.get("runtime_policy") or {}
        env_name = str(db_env.get("environment_name") or "unknown")

        if not spec.get("enabled", True):
            return self._decision(
                scope="tool_visibility",
                subject=spec["name"],
                decision="deny",
                risk_level=risk,
                reasons=[f"Tool '{spec['name']}' is disabled."],
                matched_rules=["tool.disabled"],
            )
        if policy == "no_tools":
            return self._decision(
                scope="tool_visibility",
                subject=spec["name"],
                decision="deny",
                risk_level=risk,
                reasons=["Current plan step does not allow tools."],
                matched_rules=["step.no_tools"],
            )
        if self._is_mutating_postgres_spec(spec) and (
            db_env.get("is_production")
            or env_name == "unknown"
            or runtime_policy.get("allow_database_writes") is False
        ):
            return self._decision(
                scope="tool_visibility",
                subject=spec["name"],
                decision="deny",
                risk_level=risk,
                reasons=[f"PostgreSQL write-capable tool hidden for target environment '{env_name}'."],
                matched_rules=["environment.write_tools_hidden"],
            )
        phase = str((step or {}).get("phase", ""))
        if phase and spec["allowed_phases"] and phase not in spec["allowed_phases"]:
            return self._decision(
                scope="tool_visibility",
                subject=spec["name"],
                decision="deny",
                risk_level=risk,
                reasons=[f"Tool '{spec['name']}' is not visible during phase '{phase}'."],
                matched_rules=["tool.phase"],
            )
        if spec["allowed_policies"] and policy not in spec["allowed_policies"]:
            return self._decision(
                scope="tool_visibility",
                subject=spec["name"],
                decision="deny",
                risk_level=risk,
                reasons=[f"Tool '{spec['name']}' is not visible under policy '{policy}'."],
                matched_rules=["tool.policy"],
            )
        if policy == "read_only_tools" and not capability.get("read_only"):
            return self._decision(
                scope="tool_visibility",
                subject=spec["name"],
                decision="deny",
                risk_level=risk,
                reasons=["Read-only step hides non-read-only tools."],
                matched_rules=["step.read_only_visibility"],
            )
        return self._decision(
            scope="tool_visibility",
            subject=spec["name"],
            decision="allow",
            risk_level=risk,
            reasons=["Tool is visible for the current step."],
            matched_rules=["tool.visible"],
        )

    def evaluate_tool_call(
        self,
        tool_call: dict[str, Any],
        spec: RegisteredToolSpec | None,
    ) -> tuple[SecurityPolicyDecision, SQLSafetyReport | None, ApprovalBinding | None]:
        tool_name = str(tool_call.get("name", "unknown"))
        call_id = str(tool_call.get("id", ""))
        args = tool_call.get("args") or {}
        step, policy = self._current_step_and_policy()
        risk = str((spec or {}).get("capability", {}).get("risk_level", (step or {}).get("risk_level", "low")))
        sql = _sql_from_args(args) or _project_sql_from_tool_call(tool_name, args)
        sql_report = build_sql_safety_report(sql, allow_explain_analyze=True) if sql else None
        if sql_report:
            sql_report.update(
                {
                    "call_id": call_id,
                    "tool_call_id": call_id,
                    "tool_name": tool_name,
                    "step_id": (step or {}).get("id"),
                    "source": "tool_policy_gate",
                }
            )
            risk = _risk_max(risk, sql_report["risk_level"])

        if policy == "no_tools":
            return (
                self._decision(
                    scope="tool_call",
                    subject=tool_name,
                    decision="deny",
                    risk_level=risk,
                    reasons=["Current plan step does not allow tool calls."],
                    matched_rules=["step.no_tools"],
                ),
                sql_report,
                None,
            )

        if policy == "read_only_tools" and self._is_write_intent(tool_call, spec):
            return (
                self._decision(
                    scope="tool_call",
                    subject=tool_name,
                    decision="deny",
                    risk_level=risk,
                    reasons=["Current plan step is read-only; write-capable tool call blocked."],
                    matched_rules=["step.read_only_call"],
                ),
                sql_report,
                None,
            )

        violation = self._safety_memory_violation(tool_call, spec)
        if violation:
            return (
                self._decision(
                    scope="tool_call",
                    subject=tool_name,
                    decision="deny",
                    risk_level=risk,
                    reasons=[violation],
                    matched_rules=["memory.prohibition"],
                ),
                sql_report,
                None,
            )

        if spec is None:
            return (
                self._decision(
                    scope="tool_call",
                    subject=tool_name,
                    decision="deny",
                    risk_level="high",
                    reasons=[f"Tool '{tool_name}' is not registered."],
                    matched_rules=["tool.unregistered"],
                ),
                sql_report,
                None,
            )

        if not spec.get("enabled", True):
            return (
                self._decision(
                    scope="tool_call",
                    subject=tool_name,
                    decision="deny",
                    risk_level=risk,
                    reasons=[f"Tool '{tool_name}' is disabled."],
                    matched_rules=["tool.disabled"],
                ),
                sql_report,
                None,
            )

        capability = spec["capability"]
        if policy == "read_only_tools":
            if capability["domain"] == "postgresql" and not capability.get("read_only"):
                return (
                    self._decision(
                        scope="tool_call",
                        subject=tool_name,
                        decision="deny",
                        risk_level=risk,
                        reasons=["Current plan step allows only read-only database tools."],
                        matched_rules=["step.read_only_postgres"],
                    ),
                    sql_report,
                    None,
                )

        db_env = self.state.get("database_environment") or {}
        runtime_policy = self.state.get("runtime_policy") or {}
        env_name = str(db_env.get("environment_name") or "unknown")
        if self._is_mutating_postgres_spec(spec) and (
            db_env.get("is_production")
            or env_name == "unknown"
            or runtime_policy.get("allow_database_writes") is False
        ):
            return (
                self._decision(
                    scope="tool_call",
                    subject=tool_name,
                    decision="deny",
                    risk_level=risk,
                    reasons=[f"Runtime policy blocks PostgreSQL write-capable tools for target environment '{env_name}'."],
                    matched_rules=["environment.write_blocked"],
                ),
                sql_report,
                None,
            )

        phase = str((step or {}).get("phase", ""))
        if phase and spec["allowed_phases"] and phase not in spec["allowed_phases"]:
            return (
                self._decision(
                    scope="tool_call",
                    subject=tool_name,
                    decision="deny",
                    risk_level=risk,
                    reasons=[f"Tool '{tool_name}' is not allowed during phase '{phase}'."],
                    matched_rules=["tool.phase"],
                ),
                sql_report,
                None,
            )

        if spec["allowed_policies"] and policy not in spec["allowed_policies"]:
            return (
                self._decision(
                    scope="tool_call",
                    subject=tool_name,
                    decision="deny",
                    risk_level=risk,
                    reasons=[f"Tool '{tool_name}' is not allowed by current policy '{policy}'."],
                    matched_rules=["tool.policy"],
                ),
                sql_report,
                None,
            )

        if sql_report:
            sql_decision = self.evaluate_sql_execution(
                tool_name=tool_name,
                sql_report=sql_report,
                spec=spec,
                step=step,
            )
            if sql_decision["decision"] in {"deny", "require_clarification"}:
                return sql_decision, sql_report, None
            if sql_decision["decision"] == "require_approval":
                return sql_decision, sql_report, None

        approval_binding = self._approved_binding(step=step, tool_name=tool_name, sql_report=sql_report)
        requires_approval = bool(capability.get("requires_approval")) or (
            policy == "write_tools_after_approval" and self._is_write_intent(tool_call, spec)
        )
        if requires_approval and approval_binding is None:
            payload = {
                "call_id": call_id,
                "step_id": (step or {}).get("id"),
                "tool_name": tool_name,
                "tool_args": args,
                "target_environment": env_name,
                "target_database": db_env.get("target_database"),
            }
            if sql_report:
                payload.update(
                    {
                        "sql_hash": sql_report["sql_hash"],
                        "sql_preview": sql_report["normalized_sql_preview"],
                        "sql_classification": sql_report["classification"],
                        "impact_summary": "Impact must be reviewed before execution.",
                        "rollback_summary": "Rollback or backup plan must be reviewed before execution.",
                        "verification_criteria": (step or {}).get("success_criteria", []),
                    }
                )
            return (
                self._decision(
                    scope="tool_call",
                    subject=tool_name,
                    decision="require_approval",
                    risk_level=risk,
                    reasons=["Current write-capable tool call requires explicit approval before execution."],
                    matched_rules=["approval.required"],
                    approval_payload=payload,
                ),
                sql_report,
                None,
            )

        return (
            self._decision(
                scope="tool_call",
                subject=tool_name,
                decision="allow",
                risk_level=risk,
                reasons=["Allowed by current security policy."],
                matched_rules=["tool_call.allowed"],
            ),
            sql_report,
            approval_binding,
        )

    def evaluate_sql_execution(
        self,
        *,
        tool_name: str,
        sql_report: SQLSafetyReport,
        spec: RegisteredToolSpec | None = None,
        step: TaskStep | None = None,
    ) -> SecurityPolicyDecision:
        capability = (spec or {}).get("capability", {})
        risk = _risk_max(str(capability.get("risk_level", "low")), sql_report["risk_level"])
        classification = sql_report["classification"]
        db_env = self.state.get("database_environment") or {}
        runtime_policy = self.state.get("runtime_policy") or {}
        env_name = str(db_env.get("environment_name") or "unknown")

        if sql_report["contains_multiple_statements"] and tool_name not in {"postgres_hypothetical_index_test"}:
            return self._decision(
                scope="sql_execution",
                subject=tool_name,
                decision="deny",
                risk_level=risk,
                reasons=["Multiple SQL statements are not allowed in one tool call."],
                matched_rules=["sql.multiple_statements"],
            )

        if sql_report["denial_reason"] and tool_name not in POSTGRES_DIAGNOSTIC_TOOLS_ALLOW_WRITE_SQL_INPUT:
            return self._decision(
                scope="sql_execution",
                subject=tool_name,
                decision="deny",
                risk_level=risk,
                reasons=[sql_report["denial_reason"]],
                matched_rules=["sql.denial_reason"],
            )

        if classification == "unknown":
            return self._decision(
                scope="sql_execution",
                subject=tool_name,
                decision="require_clarification",
                risk_level="high",
                reasons=["SQL classification is unknown; ask the user to clarify or rewrite SQL."],
                matched_rules=["sql.unknown"],
            )

        is_mutating = classification in {
            "data_change",
            "schema_change",
            "permission_change",
            "maintenance",
            "transaction_control",
        }
        if (
            capability.get("domain") == "postgresql"
            and capability.get("read_only")
            and is_mutating
            and tool_name not in POSTGRES_DIAGNOSTIC_TOOLS_ALLOW_WRITE_SQL_INPUT
        ):
            return self._decision(
                scope="sql_execution",
                subject=tool_name,
                decision="deny",
                risk_level=risk,
                reasons=[f"Read-only PostgreSQL tool cannot execute {classification} SQL."],
                matched_rules=["sql.read_only_tool"],
            )

        if is_mutating and tool_name not in POSTGRES_DIAGNOSTIC_TOOLS_ALLOW_WRITE_SQL_INPUT:
            if db_env.get("is_production") or env_name == "unknown" or runtime_policy.get("allow_database_writes") is False:
                return self._decision(
                    scope="sql_execution",
                    subject=tool_name,
                    decision="deny",
                    risk_level=risk,
                    reasons=[f"PostgreSQL write SQL is blocked for target environment '{env_name}'."],
                    matched_rules=["environment.sql_write_blocked"],
                )
            binding = self._approved_binding(step=step, tool_name=tool_name, sql_report=sql_report)
            if binding is None:
                return self._decision(
                    scope="sql_execution",
                    subject=tool_name,
                    decision="require_approval",
                    risk_level=risk,
                    reasons=["SQL requires approval bound to step, environment, and SQL hash."],
                    matched_rules=["approval.sql_hash_required"],
                    approval_payload={
                        "step_id": (step or {}).get("id"),
                        "tool_name": tool_name,
                        "target_environment": env_name,
                        "target_database": db_env.get("target_database"),
                        "sql_hash": sql_report["sql_hash"],
                        "sql_preview": sql_report["normalized_sql_preview"],
                        "sql_classification": classification,
                        "risk_level": risk,
                        "impact_summary": "Impact must be reviewed before execution.",
                        "rollback_summary": "Rollback or backup plan must be reviewed before execution.",
                        "verification_criteria": (step or {}).get("success_criteria", []),
                    },
                )

        return self._decision(
            scope="sql_execution",
            subject=tool_name,
            decision="allow",
            risk_level=risk,
            reasons=["SQL is allowed by current security policy."],
            matched_rules=["sql.allowed"],
        )

    def output_handling_decision(
        self,
        *,
        tool_name: str,
        result: dict[str, Any],
    ) -> SecurityPolicyDecision:
        policy = self.state.get("output_safety_policy") or self.default_output_policy()
        reasons = []
        matched = []
        payload = result.get("payload") if isinstance(result, dict) else {}
        masked = result.get("sensitive_fields_masked") or []
        truncated = bool(result.get("truncated"))
        row_count = result.get("row_count")
        if masked:
            reasons.append(f"Masked sensitive fields: {', '.join(str(item) for item in masked[:5])}.")
            matched.append("output.mask_sensitive_fields")
        if truncated:
            reasons.append("Tool output was truncated before entering context.")
            matched.append("output.truncated")
        if isinstance(row_count, int) and row_count > policy["max_rows"]:
            reasons.append(f"Row count {row_count} exceeds model-safe max rows {policy['max_rows']}.")
            matched.append("output.max_rows")
        if isinstance(payload, dict) and len(repr(payload)) > policy["max_chars"]:
            reasons.append("Payload exceeds model-safe max chars; artifact reference should be used.")
            matched.append("output.max_chars")
        if not reasons:
            reasons.append("Output is within safety policy.")
            matched.append("output.safe")
        return self._decision(
            scope="output_handling",
            subject=tool_name,
            decision="allow",
            risk_level="low",
            reasons=reasons,
            matched_rules=matched,
        )
