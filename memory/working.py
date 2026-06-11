"""Working memory: extracted key-value facts managed by the LLM.

The LLM is prompted to extract and maintain key facts as structured key:value
pairs stored in state["working_memory"]. This gives the agent persistent
awareness of:
  - user_goal: what the user wants to accomplish
  - current_context: active file, directory, project
  - decisions: key decisions made during the conversation
  - blockers: current errors or unresolved issues
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# ── Prompt injected into system message ──────────────────────

WORKING_MEMORY_SYSTEM_PROMPT = """\
## Working Memory

Throughout this conversation, maintain a "working memory" of key facts.
After each response, I will update the following fields based on new information:

- **user_goal**: What the user is trying to accomplish (one sentence).
- **current_context**: Current file, directory, project, or environment context.
- **decisions**: Key decisions or choices made.
- **blockers**: Current errors, unresolved issues, or items awaiting human input.

Only update a field when new information is available. Keep values concise."""

# ── Extraction prompt ────────────────────────────────────────

WORKING_MEMORY_EXTRACTION_PROMPT = """\
Based on the latest interaction, extract or update the working memory facts.
Respond with ONLY a JSON object containing the fields that should be updated:

```json
{
  "user_goal": "<updated or unchanged>",
  "current_context": "<updated or unchanged>",
  "decisions": "<updated or unchanged>",
  "blockers": "<updated or unchanged>"
}
```

Only include fields that have changed. Omit fields that remain the same."""


# ── Default initial values ───────────────────────────────────

def get_default_working_memory() -> dict[str, str]:
    """Return the initial working memory state."""
    return {
        "user_goal": "Not yet specified",
        "current_context": "Starting session",
        "decisions": "None yet",
        "blockers": "None",
    }


# ── Formatting ───────────────────────────────────────────────

def format_working_memory(working_memory: dict[str, str]) -> str:
    """Format working memory as a string for injection into system prompt."""
    if not working_memory:
        return ""

    lines = ["## Current Working Memory"]
    labels = {
        "user_goal": "Goal",
        "current_context": "Context",
        "decisions": "Decisions",
        "blockers": "Blockers",
    }
    for key, label in labels.items():
        value = working_memory.get(key, "")
        if value:
            lines.append(f"- {label}: {value}")

    return "\n".join(lines)


def update_working_memory(
    current: dict[str, str],
    updates: dict[str, Any],
) -> dict[str, str]:
    """Merge updates into working memory, keeping existing values for omitted keys."""
    merged = dict(current)
    for key in ("user_goal", "current_context", "decisions", "blockers"):
        if key in updates and updates[key] is not None:
            merged[key] = str(updates[key])
    return merged
