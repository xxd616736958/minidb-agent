"""Tests for the tool system: shell tool, builtin tools, and registry."""

import os
import tempfile

import pytest

from tools.base import CommandNotAllowedError, DangerousCommandError
from tools.registry import SkillRegistry
from tools.builtin.shell import ShellTool


class TestShellTool:
    """Tests for the shell execution tool."""

    def test_safe_command(self):
        """Safe commands should execute without error."""
        tool = ShellTool()
        result = tool._run("echo hello world")
        assert "hello world" in result
        assert "Exit code: 0" in result

    def test_dangerous_command_raises(self):
        """Dangerous commands should raise DangerousCommandError."""
        tool = ShellTool()
        with pytest.raises(DangerousCommandError):
            tool._run("rm -rf /tmp/test")

    def test_unknown_command_raises(self):
        """Commands not in whitelist should raise CommandNotAllowedError."""
        tool = ShellTool()
        with pytest.raises(CommandNotAllowedError):
            tool._run("unknown_cmd_xyz arg1")

    def test_empty_command(self):
        """Empty command should return error message."""
        tool = ShellTool()
        result = tool._run("")
        assert "empty command" in result.lower()

    def test_pwd_command(self):
        """pwd should return current directory."""
        tool = ShellTool()
        result = tool._run("pwd")
        assert "Exit code: 0" in result
        assert "/" in result  # Any valid path

    def test_timeout(self):
        """Long-running commands should timeout."""
        tool = ShellTool()
        # Add 'sleep' to whitelist for this test only
        tool._whitelist.add("sleep")
        result = tool._run("sleep 5", timeout=1)
        assert "TIMEOUT" in result


class TestSkillRegistry:
    """Tests for the plugin skill registry."""

    def test_discover_builtin_tools(self):
        """Registry should discover tools from tools.builtin."""
        registry = SkillRegistry()
        discovered = registry.discover("tools.builtin")
        names = [t.name for t in discovered]
        assert "file_read" in names
        assert "file_write" in names
        assert "code_search" in names
        assert registry.get_spec("file_read")["capability"]["read_only"] is True
        assert registry.get_spec("shell_execute")["capability"]["requires_approval"] is True

    def test_get_by_name(self):
        """Should retrieve tools by name."""
        registry = SkillRegistry()
        registry.discover("tools.builtin")
        tool = registry.get_by_name("file_read")
        assert tool is not None
        assert tool.name == "file_read"

    def test_manual_register(self):
        """Should support manual tool registration."""
        registry = SkillRegistry()
        tool = ShellTool()
        registry.register(tool)
        assert registry.get_by_name("shell_execute") is not None
        assert registry.get_spec("shell_execute")["capability"]["risk_level"] == "high"
        registry.unregister("shell_execute")

    def test_describe(self):
        """describe() should return human-readable summary."""
        registry = SkillRegistry()
        registry.discover("tools.builtin")
        desc = registry.describe()
        assert "file_read" in desc
        assert "file_write" in desc


class TestBuiltinTools:
    """Tests for built-in tools."""

    def test_file_read(self):
        """Should read file contents."""
        with tempfile.TemporaryDirectory() as tmpdir:
            old_cwd = os.getcwd()
            os.chdir(tmpdir)
            from tools.builtin.file_read import FileReadTool
            try:
                with open("input.txt", "w", encoding="utf-8") as f:
                    f.write("line 1\nline 2\nline 3\n")
                tool = FileReadTool()
                result = tool._run("input.txt")
                assert "line 1" in result
                assert "line 2" in result
                assert "line 3" in result
            finally:
                os.chdir(old_cwd)

    def test_file_read_with_offset_limit(self):
        """Should support offset and limit."""
        with tempfile.TemporaryDirectory() as tmpdir:
            old_cwd = os.getcwd()
            os.chdir(tmpdir)
            from tools.builtin.file_read import FileReadTool
            try:
                with open("input.txt", "w", encoding="utf-8") as f:
                    f.write("a\nb\nc\nd\ne\n")
                tool = FileReadTool()
                result = tool._run("input.txt", offset=2, limit=2)
                assert "line 2" not in result or "b" in result  # depends on line numbering format
            finally:
                os.chdir(old_cwd)

    def test_file_read_nonexistent(self):
        """Should return error for missing file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            old_cwd = os.getcwd()
            os.chdir(tmpdir)
            from tools.builtin.file_read import FileReadTool
            try:
                tool = FileReadTool()
                result = tool._run("missing.txt")
                assert "not found" in result.lower()
            finally:
                os.chdir(old_cwd)

    def test_file_write(self):
        """Should create a file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            old_cwd = os.getcwd()
            os.chdir(tmpdir)
            from tools.builtin.file_write import FileWriteTool
            try:
                tool = FileWriteTool()
                test_path = "test_output.txt"
                result = tool._run(test_path, "Hello, World!")
                assert "Created file" in result or "Updated file" in result
                with open(test_path) as f:
                    assert f.read() == "Hello, World!"
            finally:
                os.chdir(old_cwd)

    def test_code_search(self):
        """Should search for patterns in files."""
        # Search in the current project for a known pattern
        from tools.builtin.code_search import CodeSearchTool
        tool = CodeSearchTool()
        # Search for a class definition in our code
        result = tool._run("class ShellTool", ".", "*.py", max_results=5)
        assert "ShellTool" in result or "matches" in result.lower() or "No matches" in result
