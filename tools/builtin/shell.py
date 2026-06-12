"""Shell execution tool with command whitelist and dangerous-command sandbox.

Key safety features:
  - Command whitelist: only explicitly allowed commands execute
  - Dangerous-command detection: rm, dd, mkfs, etc. raise DangerousCommandError
  - DangerousCommandError is caught by human_approval_node → triggers interrupt()
  - 30-second timeout per command
  - stdout/stderr captured separately, truncated at 100KB
  - Pydantic strict validation on all input parameters
"""

from __future__ import annotations

import logging
import os
import shlex
import subprocess
from typing import Optional, Type

from pydantic import BaseModel, Field

from execution.environment import ExecutionEnvironmentManager
from tools.base import (
    AgentTool,
    CommandNotAllowedError,
    DangerousCommandError,
)

logger = logging.getLogger(__name__)

# ── Pydantic input schema ────────────────────────────────────

class ShellInput(BaseModel):
    """Strict schema for shell command parameters.

    The LLM MUST produce JSON conforming to this schema.
    Pydantic validates before _run() is called — no JSON hallucination.
    """
    command: str = Field(
        description="Shell command to execute. Must start with a whitelisted command.",
        min_length=1,
        max_length=4096,
    )
    cwd: Optional[str] = Field(
        default=None,
        description="Working directory for command execution. "
                    "Defaults to the current working directory.",
    )
    timeout: float = Field(
        default=30.0,
        ge=1.0,
        le=120.0,
        description="Command timeout in seconds (1-120).",
    )


# ── Tool implementation ──────────────────────────────────────

class ShellTool(AgentTool):
    """Execute a shell command on the local system.

    Commands are validated against a configurable whitelist.
    Dangerous commands (rm, dd, mkfs, sudo, etc.) raise an error
    that triggers the human approval interrupt node.
    """

    name: str = "shell_execute"
    description: str = (
        "Execute a shell command on the local system. "
        "Use this to run terminal commands like listing files (ls), "
        "reading files (cat), searching (grep), version control (git), "
        "running scripts (python3, node), and package management (pip, npm). "
        "Dangerous commands (rm, sudo, dd) will require human approval. "
        "Always prefer this tool over trying to run commands in your response text."
    )
    args_schema: Type[BaseModel] = ShellInput
    tool_timeout: float = 60.0
    tool_domain: str = "shell"
    operation_type: str = "none"
    risk_level: str = "high"
    read_only: bool = False
    destructive: bool = True
    requires_approval: bool = True
    allowed_phases: list[str] = ["execute", "verify"]
    allowed_policies: list[str] = ["write_tools_after_approval"]
    output_type: str = "shell_result"
    result_sensitivity: str = "internal"
    supports_parallel: bool = False
    search_hint: str | None = "run local shell command"

    # ── Command classification (overridden from config at runtime) ──
    _whitelist: set[str] = set()
    _dangerous: set[str] = set()

    _MAX_OUTPUT_BYTES: int = 100_000  # 100KB max output

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # Load whitelist/dangerous from settings at init time
        from agent.config import get_settings
        settings = get_settings()
        self._whitelist = settings.command_whitelist_set
        self._dangerous = settings.dangerous_commands_set

    def _run(
        self,
        command: str,
        cwd: Optional[str] = None,
        timeout: float = 30.0,
    ) -> str:
        """Execute a shell command after safety validation.

        Args:
            command: The shell command string.
            cwd: Optional working directory.
            timeout: Timeout in seconds (1-120).

        Returns:
            Formatted string with stdout and stderr.

        Raises:
            DangerousCommandError: Command is in the dangerous list.
            CommandNotAllowedError: Command is not in the whitelist.
            subprocess.TimeoutExpired: Command exceeded timeout.
        """
        cmd_parts = shlex.split(command)
        if not cmd_parts:
            return "Error: empty command"

        base_cmd = cmd_parts[0]
        env = ExecutionEnvironmentManager()
        allowed, reason = env.shell_command_allowed(command)
        if not allowed:
            return f"COMMAND_BLOCKED: {reason}"

        # ── Stage 1: Check dangerous commands first ──────────
        if base_cmd in self._dangerous:
            msg = (
                f"DANGEROUS_COMMAND: '{command}' includes '{base_cmd}' "
                f"which is in the dangerous commands list. "
                f"This operation requires explicit human approval."
            )
            logger.warning(msg)
            raise DangerousCommandError(command, msg)

        # ── Stage 2: Check whitelist ─────────────────────────
        if base_cmd not in self._whitelist:
            msg = (
                f"COMMAND_NOT_ALLOWED: '{base_cmd}' is not in the allowed commands. "
                f"Whitelisted commands: {sorted(self._whitelist)}"
            )
            logger.warning(msg)
            raise CommandNotAllowedError(command, msg)

        # ── Stage 3: Execute ─────────────────────────────────
        try:
            work_dir = str(env.resolve_read_path(cwd)) if cwd else env.workspace_profile["default_cwd"]
        except PermissionError as e:
            return f"Error: {e}"
        max_timeout = float(env.runtime_policy.get("max_tool_duration_seconds", 120))
        effective_timeout = min(timeout, max_timeout, 120.0)
        logger.info(f"Executing shell command: {command} (cwd={work_dir})")

        try:
            result = subprocess.run(
                command,
                shell=True,
                cwd=work_dir,
                capture_output=True,
                text=True,
                timeout=effective_timeout,
            )
        except subprocess.TimeoutExpired:
            logger.error(f"Command timed out after {effective_timeout}s: {command}")
            return (
                f"TIMEOUT: Command exceeded {effective_timeout}s limit and was terminated.\n"
                f"Command: {command}"
            )

        # ── Truncate output ──────────────────────────────────
        stdout = result.stdout
        stderr = result.stderr
        stdout_truncated = len(stdout.encode()) > self._MAX_OUTPUT_BYTES
        stderr_truncated = len(stderr.encode()) > self._MAX_OUTPUT_BYTES

        if stdout_truncated:
            stdout = stdout[:self._MAX_OUTPUT_BYTES] + "\n... [stdout truncated at 100KB]"
        if stderr_truncated:
            stderr = stderr[:self._MAX_OUTPUT_BYTES] + "\n... [stderr truncated at 100KB]"

        # ── Format output ────────────────────────────────────
        lines = [
            f"Exit code: {result.returncode}",
        ]
        if stdout:
            lines.append(f"stdout:\n{stdout}")
        else:
            lines.append("stdout: (empty)")
        if stderr:
            lines.append(f"stderr:\n{stderr}")
        else:
            lines.append("stderr: (empty)")

        return "\n".join(lines)
