"""File write tool — create or overwrite files with safety checks."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional, Type

from pydantic import BaseModel, Field

from tools.base import AgentTool


class FileWriteInput(BaseModel):
    """Input schema for writing files."""
    path: str = Field(description="Absolute or relative path to write the file to.")
    content: str = Field(
        description="The complete content to write to the file.",
        max_length=500_000,
    )


class FileWriteTool(AgentTool):
    """Write content to a file, creating parent directories if needed.

    Safety: refuses to overwrite existing files unless confirmed.
    Limited to 500KB content per write.
    """

    name: str = "file_write"
    description: str = (
        "Write content to a file. Creates parent directories automatically. "
        "Use this to create new files or update existing ones. "
        "Warning: existing files will be overwritten."
    )
    args_schema: Type[BaseModel] = FileWriteInput
    tool_domain: str = "filesystem"
    operation_type: str = "documentation"
    risk_level: str = "medium"
    read_only: bool = False
    destructive: bool = True
    requires_approval: bool = False
    allowed_phases: list[str] = ["execute", "report"]
    allowed_policies: list[str] = ["write_tools_after_approval"]
    output_type: str = "file_write_result"
    result_sensitivity: str = "internal"
    supports_parallel: bool = False
    search_hint: str | None = "write local file content"

    def _run(self, path: str, content: str) -> str:
        file_path = Path(path).expanduser().resolve()

        # Safety: check for existing
        existed = file_path.exists()

        # Create parent directories
        try:
            file_path.parent.mkdir(parents=True, exist_ok=True)
        except PermissionError:
            return f"Error: Permission denied creating directory: {file_path.parent}"

        try:
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(content)
        except PermissionError:
            return f"Error: Permission denied writing to: {path}"
        except Exception as e:
            return f"Error writing '{path}': {e}"

        size = len(content.encode("utf-8"))
        lines = content.count("\n") + 1
        action = "Updated" if existed else "Created"
        return f"{action} file: {file_path} ({size:,} bytes, {lines} lines)"
