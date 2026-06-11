"""Task planning node — decomposes complex instructions into subtask DAGs.

For requests requiring 3+ logical steps, the planner produces a JSON DAG
of subtasks with dependency information. The main loop then executes them
sequentially, respecting dependencies.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from agent.config import get_settings
from agent.llm_factory import create_llm_no_tools
from agent.state import AgentState, TaskStep

logger = logging.getLogger(__name__)

# ── Planning prompt ──────────────────────────────────────────

TASK_PLANNER_SYSTEM_PROMPT = """\
You are a task planning expert. Your job is to analyze user instructions and
determine if they require multiple steps to complete.

## Instructions

1. If the user's request is **simple** (single question, single command, or
   one obvious action), respond with an EMPTY JSON array: `[]`

2. If the request is **complex** (3+ distinct logical steps, or requires
   coordination across multiple files/systems), decompose it into a DAG of
   subtasks. Output a JSON array of task objects.

## Task Object Schema
Each task must have:
```json
{
  "id": "short-unique-id",
  "description": "Clear, actionable description of what to do",
  "dependencies": ["id-of-task-that-must-complete-first"],
  "status": "pending"
}
```

## Dependency Rules
- A task with empty `dependencies` array can run immediately.
- A task with dependencies can only start after ALL its dependencies are "completed".
- The DAG must be acyclic — no circular dependencies.
- Tasks should be ordered logically: setup → main work → verification.

## Examples

**Simple request**: "What files are in the current directory?"
```json
[]
```

**Complex request**: "Set up a new Python project with a virtual environment, install FastAPI, create a basic app.py, and run it"
```json
[
  {"id": "create-dir", "description": "Create project directory structure", "dependencies": [], "status": "pending"},
  {"id": "init-venv", "description": "Create Python virtual environment", "dependencies": ["create-dir"], "status": "pending"},
  {"id": "install-deps", "description": "Install FastAPI and uvicorn via pip", "dependencies": ["init-venv"], "status": "pending"},
  {"id": "write-app", "description": "Create app.py with basic FastAPI hello world", "dependencies": ["create-dir"], "status": "pending"},
  {"id": "verify-run", "description": "Run the app and verify it starts correctly", "dependencies": ["install-deps", "write-app"], "status": "pending"}
]
```

## Current Context
{memory_context}

Analyze the user's request below and output ONLY the JSON array (no explanation).
"""


# ── Node implementation ──────────────────────────────────────

def _should_plan(state: AgentState) -> bool:
    """Determine if planning is needed for this turn."""
    messages = state.get("messages", [])

    # Only plan on the first user message (fresh conversation or new instruction)
    # If we already have a plan and tasks remaining, don't replan
    task_stack = state.get("task_stack", [])
    if task_stack:
        pending = [t for t in task_stack if t.get("status") in ("pending", "running")]
        if pending:
            return False  # Still executing existing plan

    # Count user messages — plan only on the first one
    user_count = sum(1 for m in messages if getattr(m, "type", None) == "human"
                     or isinstance(m, HumanMessage))
    return user_count <= 1


def _parse_plan_output(raw: str) -> list[dict[str, Any]]:
    """Safely parse the LLM's JSON output."""
    # Try direct JSON parse
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # Try extracting JSON from markdown code block
    match = re.search(r'```(?:json)?\s*([\s\S]*?)\s*```', raw)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass

    # Try finding array in the text
    match = re.search(r'\[[\s\S]*\]', raw)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass

    logger.warning(f"Failed to parse plan from LLM output: {raw[:200]}")
    return []


def task_planner(state: AgentState) -> dict[str, Any]:
    """Task planning node — optionally decomposes complex instructions.

    Only runs on the first message of a conversation or when
    the previous plan is fully complete.
    """
    if not _should_plan(state):
        logger.debug("Skipping task planner — not needed for this turn")
        return {}

    messages = state.get("messages", [])
    if not messages:
        return {}

    # Find the last user message
    user_msg = None
    for m in reversed(messages):
        if isinstance(m, HumanMessage) or getattr(m, "type", None) == "human":
            user_msg = m
            break

    if not user_msg:
        return {}

    user_content = str(user_msg.content) if hasattr(user_msg, "content") else str(user_msg)
    logger.info(f"Task planner analyzing: {user_content[:100]}...")

    settings = get_settings()

    try:
        llm = create_llm_no_tools(
            temperature=0.0,  # Deterministic planning
            max_tokens=1024,
        )

        response = llm.invoke([
            SystemMessage(content=TASK_PLANNER_SYSTEM_PROMPT.format(
                memory_context=state.get("working_memory", {}),
            )),
            HumanMessage(content=user_content),
        ])
    except Exception as e:
        logger.error(f"Task planner LLM call failed: {e}")
        # Non-fatal — agent can work without a plan
        return {"plan": None, "task_stack": [], "current_task_index": 0}

    raw = str(response.content) if hasattr(response, "content") else str(response)
    tasks_raw = _parse_plan_output(raw)

    if not tasks_raw:
        logger.info("Task planner: no decomposition needed (simple request)")
        return {"plan": None, "task_stack": [], "current_task_index": 0}

    # Validate and normalize tasks
    tasks: list[TaskStep] = []
    for i, t in enumerate(tasks_raw):
        tasks.append(TaskStep(
            id=t.get("id", f"task-{i}"),
            description=t.get("description", str(t)),
            status="pending",
            dependencies=t.get("dependencies", []),
            result=None,
            error=None,
        ))

    # Validate dependencies reference valid task IDs
    valid_ids = {t["id"] for t in tasks}
    for t in tasks:
        for dep_id in t.get("dependencies", []):
            if dep_id not in valid_ids:
                logger.warning(
                    f"Task '{t['id']}' depends on unknown task '{dep_id}'"
                )

    # Format plan for display
    plan_lines = [f"## Execution Plan ({len(tasks)} steps)"]
    for i, t in enumerate(tasks):
        deps = f" (after: {', '.join(t['dependencies'])})" if t["dependencies"] else ""
        plan_lines.append(f"{i+1}. [{t['id']}] {t['description']}{deps}")

    plan_display = "\n".join(plan_lines)
    logger.info(f"Task planner created plan: {len(tasks)} steps")

    return {
        "plan": plan_display,
        "task_stack": tasks,
        "current_task_index": 0,
    }
