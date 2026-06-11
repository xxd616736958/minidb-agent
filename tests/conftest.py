"""Pytest fixtures for the test suite."""

import os
import sys
import pytest

# Ensure the project root is on sys.path for imports
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)


@pytest.fixture
def temp_workspace(tmp_path):
    """Create a temporary workspace directory for tests."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    original_cwd = os.getcwd()
    os.chdir(workspace)
    yield workspace
    os.chdir(original_cwd)
