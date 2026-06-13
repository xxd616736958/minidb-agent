"""Tests for normalizing provider-specific tool-call markup."""

from langchain_core.messages import AIMessage, SystemMessage, ToolMessage

from agent.nodes.llm_node import _normalize_llm_tool_markup, _sanitize_tool_call_messages
from tools.policy import tool_call_items


def test_normalizes_deepseek_dsml_tool_markup_into_tool_calls():
    response = AIMessage(
        content=(
            '准备查询表。\n'
            '<｜｜DSML｜｜tool_calls> <｜｜DSML｜｜invoke name="postgres_list_objects"> '
            '<｜｜DSML｜｜parameter name="object_type" string="false">table</｜｜DSML｜｜parameter> '
            '<｜｜DSML｜｜parameter name="schema" string="false">public</｜｜DSML｜｜parameter> '
            '</｜｜DSML｜｜invoke> </｜｜DSML｜｜tool_calls>'
        )
    )

    normalized = _normalize_llm_tool_markup(response)

    assert normalized.content == "准备查询表。"
    assert normalized.tool_calls[0]["name"] == "postgres_list_objects"
    assert normalized.tool_calls[0]["args"] == {"object_type": "table", "schema_name": "public"}


def test_strips_dsml_markup_when_native_tool_calls_exist():
    response = AIMessage(
        content=(
            '数据库连接正常。现在查询所有表的信息。\n'
            '<｜｜DSML｜｜tool_calls> <｜｜DSML｜｜invoke name="postgres_list_objects"> '
            '<｜｜DSML｜｜parameter name="object_type" string="false">table</｜｜DSML｜｜parameter> '
            '</｜｜DSML｜｜invoke> </｜｜DSML｜｜tool_calls>'
        ),
        tool_calls=[{"name": "postgres_list_objects", "args": {"object_type": "table"}, "id": "call-1"}],
    )

    normalized = _normalize_llm_tool_markup(response)

    assert normalized.content == "数据库连接正常。现在查询所有表的信息。"
    assert normalized.tool_calls == response.tool_calls
    assert "tool_calls" not in normalized.content


def test_sanitize_strips_tool_calls_when_tool_messages_are_not_immediate():
    ai = AIMessage(
        content="",
        tool_calls=[{"name": "postgres_query_readonly", "args": {"sql": "SELECT 1"}, "id": "call-1"}],
    )
    messages = [
        ai,
        SystemMessage(content="recovery inserted before tool result"),
        ToolMessage(content="ok", name="postgres_query_readonly", tool_call_id="call-1"),
    ]

    cleaned = _sanitize_tool_call_messages(messages)

    assert cleaned[0].tool_calls == []
    assert "cancelled" in cleaned[0].content


def test_sanitize_keeps_tool_calls_with_immediate_tool_messages():
    ai = AIMessage(
        content="",
        tool_calls=[{"name": "postgres_query_readonly", "args": {"sql": "SELECT 1"}, "id": "call-1"}],
    )
    tool = ToolMessage(content="ok", name="postgres_query_readonly", tool_call_id="call-1")

    cleaned = _sanitize_tool_call_messages([ai, tool])

    assert cleaned[0].tool_calls == ai.tool_calls


def test_sanitize_strips_dict_tool_calls_when_order_is_invalid():
    messages = [
        {
            "type": "ai",
            "content": "",
            "tool_calls": [{"name": "postgres_read", "args": {"sql": "SELECT 1"}, "id": "call-1"}],
        },
        {"type": "system", "content": "recovery inserted before tool result"},
        {"type": "tool", "content": "ok", "name": "postgres_read", "tool_call_id": "call-1"},
    ]

    cleaned = _sanitize_tool_call_messages(messages)

    assert cleaned[0].tool_calls == []


def test_sanitize_keeps_dict_tool_calls_with_immediate_dict_tool_message():
    messages = [
        {
            "type": "ai",
            "content": "",
            "tool_calls": [{"name": "postgres_read", "args": {"sql": "SELECT 1"}, "id": "call-1"}],
        },
        {"type": "tool", "content": "ok", "name": "postgres_read", "tool_call_id": "call-1"},
    ]

    cleaned = _sanitize_tool_call_messages(messages)

    assert cleaned[0]["tool_calls"] == messages[0]["tool_calls"]


def test_tool_call_items_normalizes_openai_compatible_function_calls():
    message = {
        "type": "ai",
        "content": "",
        "additional_kwargs": {
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {
                        "name": "postgres_query_readonly",
                        "arguments": "{\"sql\":\"SELECT 1\"}",
                    },
                }
            ]
        },
    }

    calls = tool_call_items(message)

    assert calls == [
        {
            "name": "postgres_query_readonly",
            "args": {"sql": "SELECT 1"},
            "id": "call-1",
        }
    ]
