"""Base tool class with Pydantic strict validation.

All agent tools inherit from this base, which extends LangChain's BaseTool
with additional safety and observability features.
"""

from __future__ import annotations

import logging
from typing import Any, Type

from langchain_core.tools import BaseTool
from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)


class AgentTool(BaseTool):
    """Base class for all agent tools.

    Inherits from langchain_core.tools.BaseTool which provides:
      - args_schema: Pydantic model for strict argument validation
      - handle_tool_error: configurable error handling callback
      - name, description: tool metadata for LLM function calling
      - _run / _arun: sync/async execution entry points

    Subclasses MUST define:
      - name: str — unique tool identifier
      - description: str — natural language description for the LLM
      - args_schema: Type[BaseModel] — Pydantic model for input validation
      - _run(*args, **kwargs) -> str — synchronous execution
    """

    # Make this tool available for auto-discovery by SkillRegistry.
    # Set to False to hide from the registry (e.g., abstract bases).
    register_in_registry: bool = Field(default=True, exclude=True)

    # Timeout for tool execution in seconds.
    # Override in subclasses as needed.
    tool_timeout: float = Field(default=30.0, exclude=True)

    model_config = ConfigDict(arbitrary_types_allowed=True)

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """Auto-register concrete tool subclasses with the global registry."""
        super().__init_subclass__(**kwargs)
        # Use getattr to safely access fields during Pydantic model construction
        if getattr(cls, "register_in_registry", True):
            try:
                from tools.registry import registry  # noqa: F811
                # Only auto-register if the class has a concrete name set
                if hasattr(cls, "name") and isinstance(getattr(cls, "name", None), str):
                    pass  # Registration happens in registry.discover()
            except ImportError:
                pass


class DangerousCommandError(Exception):
    """Raised when a shell command is in the dangerous-commands list."""
    def __init__(self, command: str, message: str = ""):
        self.command = command
        self.message = message or f"DANGEROUS: '{command}' requires human approval"
        super().__init__(self.message)


class CommandNotAllowedError(Exception):
    """Raised when a shell command is not in the whitelist."""
    def __init__(self, command: str, message: str = ""):
        self.command = command
        self.message = message or f"NOT_ALLOWED: '{command}' is not in the whitelist"
        super().__init__(self.message)
