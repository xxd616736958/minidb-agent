"""Agent state schema — the single source of truth for all graph state.

All fields use Pydantic/TypedDict for strict validation.
State is persisted automatically by LangGraph checkpointing after every node.
"""

from typing import Annotated, Any, Literal, Optional
from typing_extensions import NotRequired, TypedDict
from langgraph.graph.message import add_messages
from langgraph.managed.is_last_step import IsLastStep
import operator


class TaskStep(TypedDict):
    """A single subtask in the planning DAG."""
    id: str
    description: str
    status: Literal["pending", "running", "completed", "failed", "skipped"]
    dependencies: list[str]            # IDs of tasks this step depends on
    result: Optional[str]
    error: Optional[str]

    # Optional PostgreSQL planning metadata. Kept optional so existing generic
    # tasks and tests remain compatible with the original planner shape.
    phase: NotRequired[Literal[
        "clarify",
        "observe",
        "diagnose",
        "propose",
        "approve",
        "execute",
        "verify",
        "report",
    ]]
    operation_type: NotRequired[Literal[
        "read_only",
        "diagnostic",
        "schema_change",
        "data_change",
        "permission_change",
        "backup_restore",
        "maintenance",
        "documentation",
        "none",
    ]]
    risk_level: NotRequired[Literal["low", "medium", "high", "critical"]]
    requires_approval: NotRequired[bool]
    requires_rollback_plan: NotRequired[bool]
    evidence_required: NotRequired[list[str]]
    success_criteria: NotRequired[list[str]]
    expected_tools: NotRequired[list[str]]
    tool_policy: NotRequired[Literal[
        "no_tools",
        "read_only_tools",
        "write_tools_after_approval",
    ]]


class DBTaskIntent(TypedDict):
    """Structured understanding of the user's PostgreSQL-related task.

    This is intentionally coarse-grained: intent fields route and constrain
    the workflow, while goal/targets/output_contract preserve open-ended work.
    """

    id: str
    domain: Literal["postgresql", "documentation", "code", "general", "unknown"]
    primary_intent: str
    candidate_intents: list[str]
    confidence: float

    goal: str
    user_language_summary: str

    operation_nature: Literal[
        "read_only",
        "diagnostic",
        "write_data",
        "schema_change",
        "permission_change",
        "backup_restore",
        "documentation",
        "unknown",
    ]

    target_environment: Literal["production", "staging", "dev", "local", "unknown"]
    target_database: Optional[str]
    target_objects: list[dict[str, Any]]

    input_artifacts: list[dict[str, Any]]
    output_contract: dict[str, Any]

    missing_slots: list[str]
    assumptions: list[str]
    constraints: list[str]

    risk_level: Literal["low", "medium", "high", "critical", "unknown"]
    requires_clarification: bool
    requires_approval: bool
    requires_rollback_plan: bool

    evidence_needed: list[str]
    suggested_workflow: str
    next_action: Literal[
        "ask_clarification",
        "plan",
        "read_only_observe",
        "request_approval",
        "decline",
    ]


class ClarificationRequest(TypedDict):
    """A structured request for missing user input."""

    id: str
    questions: list[str]
    missing_slots: list[str]
    reason: str
    status: Literal["pending", "answered", "cancelled"]


class DBTaskPlan(TypedDict):
    """Structured, auditable execution plan for database tasks."""

    id: str
    intent_id: str
    workflow: str
    summary: str
    status: Literal[
        "draft",
        "awaiting_approval",
        "running",
        "completed",
        "failed",
        "cancelled",
    ]
    steps: list[TaskStep]
    assumptions: list[str]
    constraints: list[str]
    global_risk_level: Literal["low", "medium", "high", "critical"]
    requires_user_confirmation: bool
    created_at: str
    updated_at: str


class DBObservation(TypedDict):
    """Structured observation derived from a database/tool result."""

    id: str
    step_id: str
    type: Literal[
        "query_result",
        "connection_status",
        "sql_classification",
        "explain_plan",
        "schema_summary",
        "object_detail",
        "index_summary",
        "top_queries",
        "health_report",
        "lock_report",
        "index_advice",
        "dry_run_report",
        "write_result",
        "maintenance_result",
        "row_count_estimate",
        "lock_wait",
        "affected_rows",
        "sql_error",
        "tool_error",
        "policy_denied",
    ]
    source_tool: str
    summary: str
    payload: dict[str, Any]
    created_at: str


class ApprovalDecision(TypedDict):
    """Recorded approval decision for a high-risk task step."""

    id: str
    step_id: str
    status: Literal["pending", "approved", "rejected", "edited", "expired"]
    risk_level: Literal["low", "medium", "high", "critical"]
    target_environment: str
    sql_preview: Optional[str]
    sql_hash: NotRequired[Optional[str]]
    impact_summary: Optional[str]
    rollback_summary: Optional[str]
    verification_criteria: NotRequired[list[str]]
    user_message: Optional[str]
    created_at: str
    resolved_at: Optional[str]


class TaskCard(TypedDict):
    """Human-facing summary of the agent's current task understanding."""

    id: str
    intent_id: str
    title: str
    goal: str
    target_environment: str
    target_database: Optional[str]
    target_objects: list[dict[str, Any]]
    risk_level: str
    expected_output: str
    missing_slots: list[str]
    assumptions: list[str]
    user_constraints: list[str]
    status: Literal["draft", "confirmed", "needs_clarification", "cancelled"]


class PlanReview(TypedDict):
    """Human-facing review record for a generated database task plan."""

    id: str
    plan_id: str
    status: Literal["pending", "approved", "rejected", "edited"]
    reviewed_steps: list[str]
    user_message: Optional[str]
    created_at: str
    resolved_at: Optional[str]


class CollaborationEvent(TypedDict):
    """Auditable event for key human-agent collaboration moments."""

    id: str
    event_type: Literal[
        "task_card_shown",
        "clarification_requested",
        "clarification_answered",
        "plan_shown",
        "plan_reviewed",
        "tool_call_shown",
        "approval_requested",
        "approval_resolved",
        "safety_block_explained",
        "result_explained",
        "task_paused",
        "task_resumed",
        "final_report_shown",
        "error_explained",
        "repair_attempted",
        "retry_scheduled",
        "repair_succeeded",
        "repair_failed",
        "user_action_required",
    ]
    step_id: Optional[str]
    summary: str
    payload_ref: Optional[str]
    created_at: str


class ApprovalCard(TypedDict):
    """Human-facing database approval card derived from pending_approval."""

    approval_id: str
    step_id: str
    tool_name: str
    target_environment: str
    target_database: Optional[str]
    sql_preview: Optional[str]
    sql_hash: Optional[str]
    risk_level: str
    impact_summary: Optional[str]
    rollback_summary: Optional[str]
    verification_criteria: list[str]
    replay_policy: str
    options: list[Literal[
        "approve",
        "reject",
        "edit",
        "dry_run_more",
        "report_only",
        "clarify",
    ]]


class UserFeedback(TypedDict):
    """Structured user feedback that can drive state, memory, or policy."""

    id: str
    feedback_type: Literal[
        "clarification_answer",
        "plan_edit",
        "approval_decision",
        "constraint",
        "preference",
        "correction",
        "stop",
        "resume",
    ]
    target_ref: Optional[str]
    content: str
    structured_delta: dict[str, Any]
    should_write_memory: bool
    created_at: str


class VerificationResult(TypedDict):
    """Result of checking a step's success criteria."""

    id: str
    step_id: str
    status: Literal["passed", "failed", "blocked", "skipped"]
    criteria_checked: list[str]
    evidence_ids: list[str]
    summary: str
    created_at: str


class ErrorRecord(TypedDict):
    """Structured error record for PostgreSQL task recovery."""

    id: str
    source: Literal[
        "llm",
        "tool",
        "postgresql",
        "safety_policy",
        "state",
        "approval",
        "user",
        "system",
    ]
    error_type: Literal[
        "connection_error",
        "auth_error",
        "permission_denied",
        "syntax_error",
        "sql_semantic_error",
        "object_not_found",
        "lock_timeout",
        "statement_timeout",
        "deadlock_detected",
        "constraint_violation",
        "policy_denied",
        "approval_missing",
        "approval_mismatch",
        "state_integrity_error",
        "tool_schema_error",
        "tool_runtime_error",
        "llm_output_error",
        "unknown",
    ]
    severity: Literal["info", "warning", "error", "critical"]
    node_name: Optional[str]
    step_id: Optional[str]
    tool_name: Optional[str]
    tool_call_id: Optional[str]
    sql_hash: Optional[str]
    sqlstate: Optional[str]
    target_environment: str
    target_database: Optional[str]
    message: str
    raw_excerpt: Optional[str]
    retryable: bool
    requires_user_action: bool
    created_at: str


class RecoveryDecision(TypedDict):
    """Decision selected by the self-repair controller."""

    id: str
    error_id: str
    action: Literal[
        "auto_retry",
        "rewrite_sql",
        "adjust_tool_args",
        "run_diagnostic_tool",
        "repair_state",
        "replan_step",
        "ask_user",
        "abort_safely",
    ]
    reason: str
    confidence: float
    safety_notes: list[str]
    requires_new_approval: bool
    next_node: Optional[str]
    created_at: str


class RecoveryAttempt(TypedDict):
    """One recovery attempt bound to an error and decision."""

    id: str
    error_id: str
    decision_id: str
    step_id: Optional[str]
    attempt_no: int
    action: str
    status: Literal["pending", "running", "succeeded", "failed", "skipped"]
    summary: str
    created_at: str
    completed_at: Optional[str]


class RetryBudget(TypedDict):
    """Retry budget scoped to step/tool/error/sql hash."""

    scope_key: str
    step_id: Optional[str]
    tool_name: Optional[str]
    error_type: str
    sql_hash: Optional[str]
    attempts: int
    max_attempts: int
    exhausted: bool
    last_error_id: Optional[str]


class StateRepairAction(TypedDict):
    """State repair action derived from StateIntegrityReport."""

    id: str
    source_report_id: str
    action_type: Literal[
        "sync_plan_stack",
        "reset_current_step",
        "expire_pending_approval",
        "regenerate_approval_card",
        "normalize_tool_result",
        "refresh_step_context",
        "mark_step_blocked",
    ]
    description: str
    status: Literal["pending", "applied", "failed", "skipped"]
    created_at: str


class ErrorReport(TypedDict):
    """Final human-facing report when recovery fails or partially succeeds."""

    id: str
    task_id: Optional[str]
    plan_id: Optional[str]
    step_id: Optional[str]
    status: Literal["recovered", "partially_recovered", "failed"]
    error_ids: list[str]
    recovery_attempt_ids: list[str]
    evidence_refs: list[str]
    user_summary: str
    next_options: list[str]
    created_at: str


class QualityGate(TypedDict):
    """Structured quality gate for task, safety, tool, CI, or review checks."""

    id: str
    gate_type: Literal[
        "task_completion",
        "tool_contract",
        "safety_regression",
        "state_integrity",
        "error_recovery",
        "report_quality",
        "ci",
        "human_review",
    ]
    target_ref: str
    required_checks: list[str]
    passed_checks: list[str]
    failed_checks: list[str]
    status: Literal["pending", "passed", "failed", "waived"]
    blocking: bool
    created_at: str


class EvaluationCase(TypedDict):
    """Standard task-level evaluation case for agent behavior."""

    id: str
    category: Literal[
        "intent",
        "planning",
        "tool_use",
        "safety",
        "postgresql_task",
        "error_recovery",
        "reporting",
    ]
    user_input: str
    initial_state: dict[str, Any]
    expected_state_assertions: list[dict[str, Any]]
    expected_output_assertions: list[dict[str, Any]]
    forbidden_actions: list[str]
    allowed_tools: list[str]
    required_evidence: list[str]
    tags: list[str]


class EvaluationResult(TypedDict):
    """Structured result for one EvaluationCase."""

    id: str
    case_id: str
    status: Literal["passed", "failed", "needs_review"]
    scores: dict[str, float]
    failed_assertions: list[str]
    evidence_refs: list[str]
    safety_blocked: bool
    requires_human_review: bool
    summary: str
    created_at: str


class ReplayCase(TypedDict):
    """Replayable task case derived from failures, feedback, or incidents."""

    id: str
    source: Literal["manual", "failed_task", "user_feedback", "production_incident"]
    input_messages: list[dict[str, Any]]
    state_snapshot_ref: Optional[str]
    tool_invocation_refs: list[str]
    expected_recovery: Optional[str]
    expected_final_status: str
    sensitivity: Literal["public", "internal", "sensitive"]
    created_at: str


class QualityReport(TypedDict):
    """Human-readable and auditable quality summary."""

    id: str
    target_ref: str
    scope: Literal["task", "module", "release", "ci_run", "replay_suite"]
    status: Literal["passed", "failed", "needs_review"]
    test_summary: dict[str, Any]
    evaluation_summary: dict[str, Any]
    safety_summary: dict[str, Any]
    uncovered_risks: list[str]
    human_review_required: bool
    recommendations: list[str]
    created_at: str


class StepContextPacket(TypedDict):
    """Current-step context packet injected into LLM and policy gates."""

    step_id: str
    phase: str
    description: str
    risk_level: str
    tool_policy: str
    success_criteria: list[str]
    user_constraints: list[str]
    relevant_observations: list[DBObservation]
    relevant_approvals: list[ApprovalDecision]
    relevant_verifications: list[VerificationResult]
    allowed_actions: list[str]
    blocked_actions: list[str]
    missing_context: list[str]


class DBWorkingSet(TypedDict):
    """Database objects and metadata currently relevant to the task."""

    target_environment: str
    target_database: Optional[str]
    schemas: list[str]
    tables: list[str]
    columns: dict[str, list[str]]
    indexes: dict[str, list[str]]
    known_queries: list[dict[str, Any]]
    row_counts: dict[str, int]
    statistics_refs: list[str]
    last_refreshed_at: str
    source_observation_ids: NotRequired[list[str]]
    stale_reason: NotRequired[Optional[str]]


class ResultDigest(TypedDict):
    """Safe digest for large query results."""

    observation_id: str
    row_count: int
    column_names: list[str]
    column_types: dict[str, str]
    sample_rows: list[dict[str, Any]]
    aggregates: dict[str, Any]
    truncation_applied: bool
    sensitive_fields_masked: list[str]


class ContextSnapshot(TypedDict):
    """Recoverable state snapshot for long-running database tasks."""

    intent_id: str
    plan_id: str
    current_step_id: Optional[str]
    user_constraints: list[str]
    observation_ids: list[str]
    approval_ids: list[str]
    verification_ids: list[str]
    db_working_set_ref: Optional[str]
    replan_trigger: Optional[str]
    created_at: str


class WorkspaceProfile(TypedDict):
    """Filesystem workspace boundaries and artifact locations."""

    root_path: str
    read_allowed_paths: list[str]
    write_allowed_paths: list[str]
    artifact_root: str
    report_root: str
    temp_root: str
    default_cwd: str
    git_repo: Optional[str]
    dirty_state_known: bool


class DatabaseEnvironmentProfile(TypedDict):
    """Safe, model-visible profile for the current PostgreSQL target."""

    environment_name: Literal["local", "dev", "staging", "production", "unknown"]
    target_database: Optional[str]
    safe_host_label: Optional[str]
    safe_user_label: Optional[str]
    access_mode: Literal["read_only", "diagnostic", "write_after_approval", "admin_maintenance"]
    is_production: bool
    default_statement_timeout_ms: int
    default_lock_timeout_ms: int
    max_result_rows: int
    allow_write_tools: bool
    require_backup_check_for_writes: bool
    credential_ref: str


class TaskWorkspace(TypedDict):
    """Per-task workspace for artifacts, reports, logs, and recovery."""

    task_id: str
    intent_id: Optional[str]
    plan_id: Optional[str]
    root_path: str
    artifact_ids: list[str]
    report_paths: list[str]
    sql_draft_paths: list[str]
    execution_log_ref: Optional[str]
    created_at: str
    updated_at: str


class ArtifactRecord(TypedDict):
    """Artifact metadata for task outputs and audit evidence."""

    id: str
    task_id: str
    kind: Literal[
        "sql_draft",
        "explain_json",
        "health_report",
        "query_result_digest",
        "approval_snapshot",
        "execution_log",
        "verification_evidence",
        "final_report",
    ]
    path: Optional[str]
    payload_ref: Optional[str]
    summary: str
    sensitivity: Literal["public", "internal", "sensitive", "secret"]
    lifecycle: Literal["ephemeral", "session", "persistent"]
    created_at: str


class RuntimePolicy(TypedDict):
    """Execution-level permissions and resource limits."""

    allow_shell_database_clients: bool
    allow_network_tools: bool
    allow_file_writes: bool
    allow_database_writes: bool
    require_approval_for_workspace_write: bool
    require_approval_for_database_write: bool
    max_tool_duration_seconds: int
    max_artifact_size_bytes: int


class StateMetadata(TypedDict):
    """Versioned state metadata for checkpoint recovery and migration."""

    schema_version: int
    session_id: str
    created_at: str
    updated_at: str
    last_node: Optional[str]
    last_transition: Optional[str]
    recovery_mode: Literal["normal", "resumed", "forked", "migrated"]


class DBTaskRuntimeState(TypedDict):
    """Current PostgreSQL task runtime snapshot derived from state."""

    intent_id: Optional[str]
    plan_id: Optional[str]
    current_step_id: Optional[str]
    current_phase: Optional[str]
    target_environment: str
    target_database: Optional[str]
    risk_level: str
    task_status: Literal["new", "planning", "running", "waiting", "blocked", "completed"]
    blocked_reason: Optional[str]


class StateIntegrityReport(TypedDict):
    """State consistency report emitted by StateValidator."""

    ok: bool
    errors: list[str]
    warnings: list[str]
    repair_actions: list[str]
    created_at: str


class ReplayPolicy(TypedDict):
    """Replay safety policy for a historical tool invocation."""

    tool_call_id: str
    replayable: bool
    reason: str
    requires_new_approval: bool


class SecurityPolicyDecision(TypedDict):
    """Decision emitted by the safety guardrail policy engine."""

    id: str
    scope: Literal[
        "tool_visibility",
        "tool_call",
        "sql_execution",
        "workspace_access",
        "output_handling",
        "state_replay",
    ]
    subject: str
    decision: Literal["allow", "deny", "require_approval", "require_clarification"]
    risk_level: Literal["low", "medium", "high", "critical"]
    reasons: list[str]
    matched_rules: list[str]
    approval_payload: Optional[dict[str, Any]]
    created_at: str


class SQLSafetyReport(TypedDict):
    """Structured safety report for SQL text before execution."""

    sql_hash: str
    normalized_sql_preview: str
    classification: Literal[
        "read_only",
        "diagnostic",
        "data_change",
        "schema_change",
        "permission_change",
        "maintenance",
        "transaction_control",
        "unsafe",
        "unknown",
    ]
    contains_multiple_statements: bool
    contains_dangerous_constructs: list[str]
    target_objects: list[dict[str, Any]]
    requires_approval: bool
    requires_rollback_plan: bool
    requires_backup_check: bool
    can_run_in_readonly_transaction: bool
    risk_level: Literal["low", "medium", "high", "critical"]
    denial_reason: Optional[str]


class ApprovalBinding(TypedDict):
    """Precise authorization boundary for one approval decision."""

    approval_id: str
    step_id: str
    tool_name: str
    target_environment: str
    target_database: Optional[str]
    sql_hash: Optional[str]
    impact_summary: str
    rollback_summary: str
    verification_criteria: list[str]
    expires_at: Optional[str]


class OutputSafetyPolicy(TypedDict):
    """Rules for model-safe tool output handling."""

    max_rows: int
    max_chars: int
    mask_sensitive_fields: bool
    sensitive_field_patterns: list[str]
    allow_raw_result_in_context: bool
    allow_raw_result_in_memory: bool
    artifact_required_for_large_output: bool


class SafetyAuditRecord(TypedDict):
    """Auditable record for one safety-relevant decision."""

    id: str
    event_type: Literal[
        "tool_visible",
        "tool_hidden",
        "tool_allowed",
        "tool_denied",
        "approval_requested",
        "approval_resolved",
        "sql_allowed",
        "sql_denied",
        "output_masked",
        "replay_blocked",
    ]
    step_id: Optional[str]
    tool_name: Optional[str]
    decision_id: Optional[str]
    summary: str
    created_at: str


class MemoryRecord(TypedDict):
    """Safe, scoped long-term memory record."""

    id: str
    kind: Literal[
        "fact",
        "preference",
        "experience",
        "assumption",
        "prohibition",
        "schema_summary",
        "task_episode",
    ]
    scope: Literal["user", "project", "database", "schema", "session", "task"]
    namespace: str
    summary: str
    payload: dict[str, Any]
    source: Literal[
        "user_confirmed",
        "tool_observed",
        "agent_inferred",
        "report_generated",
        "system_policy",
    ]
    evidence_refs: list[str]
    confidence: float
    sensitivity: Literal["public", "internal", "sensitive", "secret"]
    ttl_seconds: Optional[int]
    observed_at: str
    expires_at: Optional[str]
    supersedes: list[str]
    status: Literal["active", "deprecated", "expired", "conflicted"]


class MemoryCandidate(TypedDict):
    """Candidate long-term memory generated from verified task state."""

    id: str
    proposed_record: MemoryRecord
    reason: str
    requires_user_confirmation: bool
    write_decision: Literal["pending", "approved", "rejected", "auto_write"]


class MemoryQuery(TypedDict):
    """Structured memory retrieval query."""

    intent_type: str
    step_phase: str
    target_environment: str
    target_database: Optional[str]
    target_objects: list[str]
    risk_level: str
    allowed_scopes: list[str]
    max_sensitivity: Literal["public", "internal", "sensitive"]


class ToolCapability(TypedDict):
    """Declared capability and safety metadata for one registered tool."""

    domain: Literal[
        "postgresql",
        "filesystem",
        "shell",
        "code",
        "memory",
        "human",
        "external",
    ]
    operation_type: Literal[
        "read_only",
        "diagnostic",
        "schema_change",
        "data_change",
        "permission_change",
        "backup_restore",
        "maintenance",
        "documentation",
        "none",
    ]
    risk_level: Literal["low", "medium", "high", "critical"]
    read_only: bool
    destructive: bool
    requires_approval: bool
    requires_transaction: bool
    supports_parallel: bool


class RegisteredToolSpec(TypedDict):
    """Model-facing and policy-facing metadata for a registered tool."""

    name: str
    description: str
    args_schema: dict[str, Any]
    capability: ToolCapability
    allowed_phases: list[str]
    allowed_policies: list[str]
    output_type: str
    result_sensitivity: Literal["public", "internal", "sensitive", "secret"]
    plugin_source: Optional[str]
    enabled: bool
    search_hint: Optional[str]
    defer_loading: bool
    always_load: bool


class ToolCallPolicyDecision(TypedDict):
    """Decision made before executing one tool call."""

    call_id: str
    tool_name: str
    decision: Literal["allow", "deny", "require_approval", "require_clarification"]
    reason: str
    risk_level: str
    approval_required: bool
    approval_payload: Optional[dict[str, Any]]


class ToolInvocationRecord(TypedDict):
    """Auditable record for one tool invocation lifecycle."""

    id: str
    call_id: str
    tool_name: str
    step_id: Optional[str]
    intent_id: Optional[str]
    args_digest: dict[str, Any]
    policy_decision: ToolCallPolicyDecision
    approval_id: Optional[str]
    started_at: str
    ended_at: Optional[str]
    status: Literal["pending", "running", "succeeded", "failed", "denied", "cancelled"]
    duration_ms: Optional[int]
    result_ref: Optional[str]
    observation_ids: list[str]
    artifact_ids: NotRequired[list[str]]
    environment_summary: NotRequired[dict[str, Any]]
    error_type: Optional[str]
    error_message: Optional[str]


class ToolExecutionResult(TypedDict):
    """Structured result normalized from a tool response."""

    tool_call_id: str
    tool_name: str
    success: bool
    result_type: Literal[
        "query_result",
        "connection_status",
        "sql_classification",
        "explain_plan",
        "schema_summary",
        "object_detail",
        "index_summary",
        "top_queries",
        "health_report",
        "lock_report",
        "index_advice",
        "dry_run_report",
        "write_result",
        "maintenance_result",
        "row_count_estimate",
        "lock_wait",
        "affected_rows",
        "sql_error",
        "tool_error",
        "policy_denied",
    ]
    summary: str
    payload: dict[str, Any]
    row_count: Optional[int]
    affected_rows: Optional[int]
    sqlstate: Optional[str]
    duration_ms: int
    truncated: bool
    sensitive_fields_masked: list[str]


class AgentState(TypedDict):
    """Complete agent state — the single data structure flowing through the graph.

    Persisted to checkpointer after every node execution.
    Keyed by thread_id for multi-session isolation.
    """

    # ── Core message loop ──────────────────────────────
    # Annotated with add_messages reducer: new messages are appended,
    # ToolMessages update matching tool_call messages in place.
    messages: Annotated[list, add_messages]

    # Managed value: true when approaching the max steps / token limit.
    # Set by LangGraph runtime; used to trigger early termination.
    is_last_step: IsLastStep

    # ── Task planning ───────────────────────────────────
    # Structured task understanding produced before planning.
    current_intent: NotRequired[Optional[DBTaskIntent]]
    intent_history: NotRequired[Annotated[list[DBTaskIntent], operator.add]]
    pending_clarification: NotRequired[Optional[ClarificationRequest]]
    confirmed_context: NotRequired[dict[str, Any]]
    selected_workflow: NotRequired[Optional[str]]
    db_task_plan: NotRequired[Optional[DBTaskPlan]]
    plan_history: NotRequired[Annotated[list[DBTaskPlan], operator.add]]
    replan_trigger: NotRequired[Optional[str]]
    task_card: NotRequired[Optional[TaskCard]]
    plan_review: NotRequired[Optional[PlanReview]]
    approval_card: NotRequired[Optional[ApprovalCard]]
    collaboration_events: NotRequired[Annotated[list[CollaborationEvent], operator.add]]
    user_feedback: NotRequired[Annotated[list[UserFeedback], operator.add]]
    current_step_id: NotRequired[Optional[str]]
    loop_status: NotRequired[Literal[
        "running",
        "waiting_for_user",
        "waiting_for_approval",
        "replanning",
        "completed",
        "blocked",
    ]]
    db_observations: NotRequired[Annotated[list[DBObservation], operator.add]]
    approval_decisions: NotRequired[Annotated[list[ApprovalDecision], operator.add]]
    verification_results: NotRequired[Annotated[list[VerificationResult], operator.add]]
    pending_approval: NotRequired[Optional[ApprovalDecision]]
    policy_violation: NotRequired[Optional[dict[str, Any]]]
    step_context: NotRequired[Optional[StepContextPacket]]
    db_working_set: NotRequired[Optional[DBWorkingSet]]
    result_digests: NotRequired[Annotated[list[ResultDigest], operator.add]]
    context_snapshots: NotRequired[Annotated[list[ContextSnapshot], operator.add]]
    user_constraints: NotRequired[list[str]]
    context_token_budget: NotRequired[int]
    memory_candidates: NotRequired[Annotated[list[MemoryCandidate], operator.add]]
    retrieved_memories: NotRequired[list[MemoryRecord]]
    memory_records_written: NotRequired[Annotated[list[MemoryRecord], operator.add]]
    available_tools: NotRequired[list[str]]
    available_tool_specs: NotRequired[list[RegisteredToolSpec]]
    tool_policy_decisions: NotRequired[Annotated[list[ToolCallPolicyDecision], operator.add]]
    tool_invocation_records: NotRequired[Annotated[list[ToolInvocationRecord], operator.add]]
    tool_execution_results: NotRequired[Annotated[list[ToolExecutionResult], operator.add]]
    workspace_profile: NotRequired[Optional[WorkspaceProfile]]
    database_environment: NotRequired[Optional[DatabaseEnvironmentProfile]]
    task_workspace: NotRequired[Optional[TaskWorkspace]]
    artifact_records: NotRequired[Annotated[list[ArtifactRecord], operator.add]]
    runtime_policy: NotRequired[Optional[RuntimePolicy]]
    security_policy_decisions: NotRequired[Annotated[list[SecurityPolicyDecision], operator.add]]
    sql_safety_reports: NotRequired[Annotated[list[SQLSafetyReport], operator.add]]
    approval_bindings: NotRequired[Annotated[list[ApprovalBinding], operator.add]]
    output_safety_policy: NotRequired[Optional[OutputSafetyPolicy]]
    safety_audit_records: NotRequired[Annotated[list[SafetyAuditRecord], operator.add]]
    state_schema_version: NotRequired[int]
    state_metadata: NotRequired[StateMetadata]
    db_task_runtime: NotRequired[Optional[DBTaskRuntimeState]]
    state_integrity_reports: NotRequired[Annotated[list[StateIntegrityReport], operator.add]]
    replay_policies: NotRequired[Annotated[list[ReplayPolicy], operator.add]]
    recovery_summary: NotRequired[Optional[str]]
    error_records: NotRequired[Annotated[list[ErrorRecord], operator.add]]
    recovery_decisions: NotRequired[Annotated[list[RecoveryDecision], operator.add]]
    recovery_attempts: NotRequired[Annotated[list[RecoveryAttempt], operator.add]]
    retry_budgets: NotRequired[list[RetryBudget]]
    state_repair_actions: NotRequired[Annotated[list[StateRepairAction], operator.add]]
    error_reports: NotRequired[Annotated[list[ErrorReport], operator.add]]
    active_recovery_decision: NotRequired[Optional[RecoveryDecision]]
    quality_gates: NotRequired[Annotated[list[QualityGate], operator.add]]
    evaluation_cases: NotRequired[Annotated[list[EvaluationCase], operator.add]]
    evaluation_results: NotRequired[Annotated[list[EvaluationResult], operator.add]]
    replay_cases: NotRequired[Annotated[list[ReplayCase], operator.add]]
    quality_reports: NotRequired[Annotated[list[QualityReport], operator.add]]

    # Human-readable plan summary injected into system prompt.
    plan: Optional[str]

    # Ordered list of subtasks forming the execution DAG.
    task_stack: list[TaskStep]

    # Index into task_stack for current execution step.
    current_task_index: int

    # ── Tool execution ─────────────────────────────────
    # Tool calls extracted from the last LLM message awaiting execution.
    tool_calls_pending: list[dict[str, Any]]

    # Accumulated tool execution results (formatted strings).
    tool_call_results: Annotated[list[str], operator.add]

    # Flag raised when shell_tool detects a dangerous command.
    dangerous_command_detected: bool

    # ── Human-in-the-loop approval ─────────────────────
    # True while the graph is paused waiting for human input.
    human_interrupt_pending: bool

    # Type of interrupt: "approve_command" | "edit_and_rerun"
    human_interrupt_type: Optional[str]

    # Serialized payload shown to the human reviewer.
    human_interrupt_payload: Optional[dict[str, Any]]

    # ── Execution control ──────────────────────────────
    # Last error message, if any node threw.
    error: Optional[str]

    # Number of consecutive retry attempts for the current operation.
    retry_count: int

    # Configurable max retries before giving up.
    max_retries: int

    # Monotonically incrementing step counter (for timeout / budget tracking).
    step_count: int

    # Unique session identifier (= thread_id in LangGraph config).
    session_id: str

    # ── Hierarchical memory ────────────────────────────
    # Short-term: recent message excerpts (managed by memory compactor).
    short_term: Annotated[list[dict[str, Any]], operator.add]

    # Working memory: extracted key-value facts (latest wins).
    working_memory: dict[str, str]

    # References into long-term store (document IDs / keys).
    long_term_refs: list[str]
