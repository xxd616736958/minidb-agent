"""State recovery node for versioned AgentState checkpoints."""

from __future__ import annotations

from agent.state import AgentState
from state_management.recovery import StateRecovery


def recover_state(state: AgentState) -> dict:
    """Normalize, recover, and validate state at graph entry."""
    return StateRecovery(state).recover()
