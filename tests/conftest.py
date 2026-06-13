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


@pytest.fixture(autouse=True)
def isolate_global_memory_store():
    """Keep persistent user memories from leaking into deterministic tests."""

    try:
        from memory.store import get_memory_store

        store = get_memory_store()
        original_records = dict(store.records)
        store.records.clear()
        yield
        store.records = original_records
    except Exception:
        yield
