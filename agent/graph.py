"""Main LangGraph StateGraph assembly and compilation.

This is the central orchestration file that:
  1. Creates the StateGraph with AgentState schema
  2. Registers all nodes and conditional edges
  3. Compiles with checkpointer (SQLite or PostgreSQL)
  4. Configures HITL breakpoints (interrupt_before)
  5. Exports the compiled graph for the LangGraph server

The exported `graph` object is what langgraph.json references:
    "graphs": { "agent": "./agent/graph.py:graph" }
"""

from __future__ import annotations

import logging

from langgraph.graph import END, START, StateGraph

from agent.config import get_settings
from agent.edges.routes import (
    LLM_REASON,
    EXECUTE_TOOLS,
    HUMAN_APPROVAL,
    ERROR_HANDLER,
    MEMORY_COMPACTOR,
    TASK_PLANNER,
    route_after_approval,
    route_after_compactor,
    route_after_error_handler,
    route_after_llm,
    route_after_planner,
    route_after_start,
    route_after_tools,
)
from agent.nodes.error_handler import error_handler
from agent.nodes.human_approval import human_approval
from agent.nodes.llm_node import llm_reason
from agent.nodes.memory_compactor import memory_compactor
from agent.nodes.task_planner import task_planner
from agent.nodes.tool_executor import execute_tools
from agent.state import AgentState
from tools.registry import registry

logger = logging.getLogger(__name__)


def build_graph() -> StateGraph:
    """Build and compile the full agent StateGraph.

    This function:
      1. Discovers all tools via SkillRegistry
      2. Creates the appropriate checkpointer
      3. Assembles all nodes and edges
      4. Compiles with HITL breakpoints

    Returns:
        A compiled LangGraph StateGraph ready for invocation.
    """
    settings = get_settings()

    # ── Discover tools ───────────────────────────────────
    logger.info("Discovering tools via SkillRegistry...")
    registry.discover("tools.builtin", "plugins")
    logger.info(f"Registered {registry.count} tools: {registry.get_names()}")

    # Note: Custom checkpointer is NOT used — LangGraph API platform
    # handles persistence automatically via POSTGRES_URI or in-memory.
    # Session isolation, time-travel, and fork/resume all work out-of-the-box.

    # ── Build graph ──────────────────────────────────────
    builder = StateGraph(AgentState)

    # Register nodes
    builder.add_node(TASK_PLANNER, task_planner)
    builder.add_node(MEMORY_COMPACTOR, memory_compactor)
    builder.add_node(LLM_REASON, llm_reason)
    builder.add_node(HUMAN_APPROVAL, human_approval)
    builder.add_node(EXECUTE_TOOLS, execute_tools)
    builder.add_node(ERROR_HANDLER, error_handler)

    # ── Add edges ────────────────────────────────────────

    # Entry: route through planner
    builder.add_conditional_edges(
        START,
        route_after_start,
        {
            "task_planner": TASK_PLANNER,
            "llm_reason": LLM_REASON,
            "human_approval": HUMAN_APPROVAL,
        },
    )

    # After planner: check errors, then compact
    builder.add_conditional_edges(
        TASK_PLANNER,
        route_after_planner,
        {
            "memory_compactor": MEMORY_COMPACTOR,
            "llm_reason": LLM_REASON,
            "error_handler": ERROR_HANDLER,
        },
    )

    # After compactor: go to LLM
    builder.add_conditional_edges(
        MEMORY_COMPACTOR,
        route_after_compactor,
        {
            "llm_reason": LLM_REASON,
            "error_handler": ERROR_HANDLER,
        },
    )

    # After LLM: approve tools, handle error, or end
    builder.add_conditional_edges(
        LLM_REASON,
        route_after_llm,
        {
            "human_approval": HUMAN_APPROVAL,
            "error_handler": ERROR_HANDLER,
            END: END,
        },
    )

    # After approval: execute or go back
    builder.add_conditional_edges(
        HUMAN_APPROVAL,
        route_after_approval,
        {
            "execute_tools": EXECUTE_TOOLS,
            "llm_reason": LLM_REASON,
            "error_handler": ERROR_HANDLER,
            END: END,
        },
    )

    # After tools: handle errors, compact, or end
    builder.add_conditional_edges(
        EXECUTE_TOOLS,
        route_after_tools,
        {
            "error_handler": ERROR_HANDLER,
            "memory_compactor": MEMORY_COMPACTOR,
            "llm_reason": LLM_REASON,
            END: END,
        },
    )

    # After error handler: retry or give up
    builder.add_conditional_edges(
        ERROR_HANDLER,
        route_after_error_handler,
        {
            "llm_reason": LLM_REASON,
            END: END,
        },
    )

    # ── Compile ──────────────────────────────────────────
    # LangGraph API platform handles persistence automatically.
    graph = builder.compile(
        # No static interrupt_before — the human_approval node uses
        # dynamic interrupt() only when dangerous commands are detected.
        # Safe tool calls pass through without pausing the graph.
    )

    logger.info("Graph compiled successfully")
    logger.info(f"  Nodes: {list(graph.nodes.keys())}")
    logger.info("  HITL: dynamic interrupt() on dangerous commands only")

    return graph


# ── Module-level graph instance ──────────────────────────────
# This is what langgraph.json references as "agent/graph.py:graph"

def _init_graph():
    """Initialize the graph on module import.

    In development (langgraph dev), this runs in the server process.
    Each request gets its own thread_id, but the graph structure is shared.
    """
    settings = get_settings()

    # Configure LangSmith if enabled
    if settings.langsmith_tracing and settings.langsmith_api_key:
        import os
        os.environ.setdefault("LANGSMITH_TRACING", "true")
        os.environ.setdefault("LANGSMITH_API_KEY", settings.langsmith_api_key)
        os.environ.setdefault("LANGSMITH_PROJECT", settings.langsmith_project)
        if settings.langsmith_endpoint:
            os.environ.setdefault("LANGSMITH_ENDPOINT", settings.langsmith_endpoint)
        logger.info(f"LangSmith tracing enabled (project: {settings.langsmith_project})")

    return build_graph()


graph = _init_graph()
