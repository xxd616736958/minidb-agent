"""Code search tool — grep for patterns in a directory."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional, Type

from pydantic import BaseModel, Field

from tools.base import AgentTool


class CodeSearchInput(BaseModel):
    """Input schema for code search."""
    pattern: str = Field(
        description="Search pattern (regex or literal string).",
        min_length=1,
        max_length=500,
    )
    directory: str = Field(
        default=".",
        description="Directory to search in (default: current working directory).",
    )
    file_pattern: str = Field(
        default="*.py",
        description="Glob pattern for files to search (default: *.py).",
    )
    max_results: int = Field(
        default=50, ge=1, le=200,
        description="Maximum number of results to return.",
    )


class CodeSearchTool(AgentTool):
    """Search for patterns in code files using grep.

    Returns matching lines with file paths and line numbers.
    """

    name: str = "code_search"
    description: str = (
        "Search for a text pattern in code files within a directory. "
        "Uses grep with regex support. Returns file paths, line numbers, "
        "and matching lines. Use this to find function definitions, "
        "variable usages, or any text pattern in a codebase."
    )
    args_schema: Type[BaseModel] = CodeSearchInput
    tool_domain: str = "code"
    operation_type: str = "read_only"
    risk_level: str = "low"
    read_only: bool = True
    destructive: bool = False
    requires_approval: bool = False
    allowed_phases: list[str] = ["observe", "diagnose", "verify", "report"]
    allowed_policies: list[str] = ["read_only_tools", "write_tools_after_approval"]
    output_type: str = "code_search_result"
    result_sensitivity: str = "internal"
    search_hint: str | None = "search code text patterns"

    def _run(
        self,
        pattern: str,
        directory: str = ".",
        file_pattern: str = "*.py",
        max_results: int = 50,
    ) -> str:
        search_dir = Path(directory).expanduser().resolve()

        if not search_dir.exists():
            return f"Error: Directory not found: {directory}"
        if not search_dir.is_dir():
            return f"Error: '{directory}' is not a directory."

        try:
            result = subprocess.run(
                [
                    "grep", "-rn", "--include", file_pattern,
                    "-m", str(max_results),
                    pattern,
                    str(search_dir),
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
        except subprocess.TimeoutExpired:
            return "Error: Search timed out after 15 seconds."

        if result.returncode == 1:
            return f"No matches found for '{pattern}' in {search_dir}/{file_pattern}"

        if result.returncode > 1:
            return f"Error executing grep: {result.stderr.strip()}"

        output = result.stdout.strip()
        lines = output.split("\n")
        return f"Found {len(lines)} matches for '{pattern}':\n\n{output}"
