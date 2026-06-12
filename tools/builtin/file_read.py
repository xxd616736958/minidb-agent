"""File read tool — read file contents with line-range support."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional, Type

from pydantic import BaseModel, Field

from tools.base import AgentTool


class FileReadInput(BaseModel):
    """Input schema for reading files."""
    path: str = Field(description="Absolute or relative path to the file to read.")
    offset: Optional[int] = Field(
        default=None, ge=1,
        description="Line number to start reading from (1-indexed)."
    )
    limit: Optional[int] = Field(
        default=None, ge=1, le=500,
        description="Maximum number of lines to read (max 500)."
    )


class FileReadTool(AgentTool):
    """Read a file from the filesystem.

    Supports reading entire files or specific line ranges.
    Automatically truncates large files.
    """

    name: str = "file_read"
    description: str = (
        "Read the contents of a file at the given path. "
        "Optionally specify an offset line and limit to read a specific range. "
        "Use this to inspect file contents before editing."
    )
    args_schema: Type[BaseModel] = FileReadInput
    tool_domain: str = "filesystem"
    operation_type: str = "read_only"
    risk_level: str = "low"
    read_only: bool = True
    destructive: bool = False
    requires_approval: bool = False
    allowed_phases: list[str] = ["observe", "diagnose", "verify", "report"]
    allowed_policies: list[str] = ["read_only_tools", "write_tools_after_approval"]
    output_type: str = "file_content"
    result_sensitivity: str = "internal"
    search_hint: str | None = "inspect local file contents"

    _MAX_LINES: int = 500
    _MAX_FILE_SIZE: int = 1_000_000  # 1MB

    def _run(
        self,
        path: str,
        offset: Optional[int] = None,
        limit: Optional[int] = None,
    ) -> str:
        file_path = Path(path).expanduser().resolve()

        if not file_path.exists():
            return f"Error: File not found: {path}"

        if file_path.is_dir():
            return f"Error: '{path}' is a directory, not a file."

        file_size = file_path.stat().st_size
        if file_size > self._MAX_FILE_SIZE:
            return (
                f"Error: File is {file_size:,} bytes (max {self._MAX_FILE_SIZE:,}). "
                f"Use offset/limit to read a smaller portion."
            )

        try:
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except Exception as e:
            return f"Error reading '{path}': {e}"

        total_lines = len(lines)
        start = (offset - 1) if offset else 0
        end = min(start + (limit or self._MAX_LINES), total_lines)
        selected = lines[start:end]

        output_lines = []
        for i, line in enumerate(selected, start=start + 1):
            output_lines.append(f"{i:6}\t{line.rstrip()}")

        header = f"File: {file_path} (lines {start+1}-{end} of {total_lines})\n"
        return header + "\n".join(output_lines)
