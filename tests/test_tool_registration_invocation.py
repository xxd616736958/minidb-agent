"""Tests for tool registration metadata, dynamic tool pools, and policy gate."""

from langchain_core.messages import AIMessage, ToolMessage

from agent.nodes.agent_loop import normalize_observation, tool_policy_gate
from agent.nodes.tool_executor import _tool_execution_result
from memory.schema import make_memory_record
from tools.builtin.code_search import CodeSearchTool
from tools.builtin.file_read import FileReadTool
from tools.builtin.file_write import FileWriteTool
from tools.builtin.shell import ShellTool
from tools.policy import evaluate_tool_call
from tools.registry import SkillRegistry, registry


def setup_function():
    registry.clear()


def _step(**extra):
    step = {
        "id": "observe",
        "description": "Observe",
        "status": "running",
        "dependencies": [],
        "result": None,
        "error": None,
        "phase": "observe",
        "operation_type": "diagnostic",
        "risk_level": "low",
        "requires_approval": False,
        "requires_rollback_plan": False,
        "evidence_required": [],
        "success_criteria": ["done"],
        "expected_tools": [],
        "tool_policy": "read_only_tools",
    }
    step.update(extra)
    return step


def _state(**extra):
    state = {
        "messages": [],
        "task_stack": [_step()],
        "current_task_index": 0,
        "current_step_id": "observe",
        "current_intent": {
            "id": "intent-1",
            "domain": "postgresql",
            "primary_intent": "performance_diagnosis",
            "target_environment": "staging",
            "target_database": "app",
            "target_objects": [{"type": "table", "name": "orders"}],
            "risk_level": "low",
        },
        "approval_decisions": [],
        "retrieved_memories": [],
    }
    state.update(extra)
    return state


def test_registry_builds_tool_specs_for_registered_tools():
    registry = SkillRegistry()
    registry.register(FileReadTool())
    registry.register(FileWriteTool())
    registry.register(ShellTool())

    assert registry.get_spec("file_read")["capability"]["read_only"] is True
    assert registry.get_spec("file_write")["capability"]["destructive"] is True
    assert registry.get_spec("shell_execute")["capability"]["requires_approval"] is True


def test_dynamic_tool_pool_hides_write_tools_in_read_only_step():
    registry = SkillRegistry()
    registry.register(FileReadTool())
    registry.register(FileWriteTool())
    registry.register(CodeSearchTool())

    tools, specs = registry.get_for_state(_state())
    names = {tool.name for tool in tools}

    assert "file_read" in names
    assert "code_search" in names
    assert "file_write" not in names
    assert all(spec["capability"]["read_only"] for spec in specs)


def test_policy_gate_denies_tool_not_allowed_by_phase():
    registry.register(FileWriteTool())
    tool_call = {"name": "file_write", "args": {"path": "x", "content": "y"}, "id": "call-1"}

    decision = evaluate_tool_call(_state(), tool_call)

    assert decision["decision"] == "deny"
    assert "not allowed" in decision["reason"] or "read-only" in decision["reason"]


def test_policy_gate_requires_approval_for_shell_execute_step():
    registry.register(ShellTool())
    tool_call = {"name": "shell_execute", "args": {"command": "ls"}, "id": "call-2"}
    state = _state(
        task_stack=[
            _step(
                id="execute",
                phase="execute",
                tool_policy="write_tools_after_approval",
                risk_level="high",
                requires_approval=True,
            )
        ],
        current_step_id="execute",
    )

    decision = evaluate_tool_call(state, tool_call)

    assert decision["decision"] == "require_approval"
    assert decision["approval_required"] is True


def test_tool_policy_gate_records_structured_decisions_and_invocations():
    registry.register(FileWriteTool())
    msg = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "file_write",
                "args": {"path": "x", "content": "y"},
                "id": "call-3",
            }
        ],
    )

    result = tool_policy_gate(_state(messages=[msg]))

    assert result["policy_violation"]
    assert result["tool_policy_decisions"][0]["decision"] == "deny"
    assert result["tool_invocation_records"][0]["call_id"] == "call-3"


def test_safety_memory_denies_write_even_when_step_would_allow():
    registry.register(ShellTool())
    safety = make_memory_record(
        kind="prohibition",
        scope="user",
        namespace="safety",
        summary="User requires read-only PostgreSQL work",
        source="user_confirmed",
        sensitivity="internal",
    )
    state = _state(
        task_stack=[
            _step(
                id="execute",
                phase="execute",
                tool_policy="write_tools_after_approval",
                requires_approval=True,
            )
        ],
        current_step_id="execute",
        approval_decisions=[
            {
                "id": "approval-1",
                "step_id": "execute",
                "status": "approved",
                "risk_level": "high",
                "target_environment": "staging",
                "sql_preview": None,
                "impact_summary": None,
                "rollback_summary": None,
                "user_message": None,
                "created_at": "now",
                "resolved_at": "now",
            }
        ],
        retrieved_memories=[safety],
    )
    tool_call = {"name": "shell_execute", "args": {"command": "psql -c 'UPDATE orders SET id=id'"}, "id": "call-4"}

    decision = evaluate_tool_call(state, tool_call)

    assert decision["decision"] == "deny"
    assert "SafetyMemory" in decision["reason"]


def test_tool_execution_result_feeds_normalize_observation():
    msg = ToolMessage(
        content="EXPLAIN SELECT * FROM orders",
        name="postgres_explain",
        tool_call_id="call-5",
    )
    result = _tool_execution_result(msg, __import__("datetime").datetime.now(__import__("datetime").timezone.utc))
    state = _state(tool_execution_results=[result])

    update = normalize_observation(state)

    assert update["db_observations"][0]["type"] == "explain_plan"
    assert update["db_observations"][0]["payload"]["tool_call_id"] == "call-5"
