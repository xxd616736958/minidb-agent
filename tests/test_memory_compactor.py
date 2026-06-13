from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from agent.nodes.memory_compactor import memory_compactor


def test_memory_compactor_skips_running_tool_step(monkeypatch):
    monkeypatch.setattr("agent.nodes.memory_compactor.get_settings", lambda: type("Settings", (), {"memory_compact_threshold": 1})())
    messages = [
        HumanMessage(content="x" * 1000),
        AIMessage(content="planning"),
        HumanMessage(content="x" * 1000),
        AIMessage(content="tool", tool_calls=[{"name": "postgres_top_queries", "args": {}, "id": "call-1"}]),
        ToolMessage(content="{}", name="postgres_top_queries", tool_call_id="call-1"),
        AIMessage(content="next"),
        HumanMessage(content="more"),
        AIMessage(content="more"),
        HumanMessage(content="more"),
        AIMessage(content="more"),
    ]
    state = {
        "messages": messages,
        "loop_status": "running",
        "current_step_id": "collect-optimization-evidence",
        "task_stack": [
            {
                "id": "collect-optimization-evidence",
                "status": "running",
                "phase": "observe",
            }
        ],
    }

    assert memory_compactor(state) == {}
