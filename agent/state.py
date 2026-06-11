"""Agent state schema — the single source of truth for all graph state.

All fields use Pydantic/TypedDict for strict validation.
State is persisted automatically by LangGraph checkpointing after every node.
"""

from typing import Annotated, Any, Literal, Optional
from typing_extensions import TypedDict
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
    # Human-readable plan summary injected into system prompt.
    plan: Optional[str]

    # Ordered list of subtasks forming the execution DAG.
    task_stack: Annotated[list[TaskStep], operator.add]

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
