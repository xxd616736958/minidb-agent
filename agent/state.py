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
