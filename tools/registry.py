"""Plugin Skill Registry — auto-discovers and registers tools.

Uses pkgutil + inspect to scan designated packages for BaseTool subclasses.
New tools added to tools/builtin/ or plugins/ are automatically discovered
on initialization — no graph code changes needed.
"""

from __future__ import annotations

import importlib
import inspect
import logging
import pkgutil
from pathlib import Path
from typing import Optional

from langchain_core.tools import BaseTool

from agent.state import AgentState, RegisteredToolSpec
from tools.catalog import ToolCatalog, build_tool_pool

logger = logging.getLogger(__name__)


class SkillRegistry:
    """Singleton registry that discovers and manages agent tools.

    Usage:
        registry = SkillRegistry()
        registry.discover("tools.builtin", "plugins")
        tools = registry.get_all()  # -> list[BaseTool]
        tool = registry.get_by_name("shell_execute")  # -> BaseTool | None
    """

    def __init__(self) -> None:
        self._tools: dict[str, BaseTool] = {}
        self.catalog = ToolCatalog()

    # ── Discovery ─────────────────────────────────────────

    def discover(self, *package_names: str) -> list[BaseTool]:
        """Scan packages for BaseTool subclasses and register them.

        Args:
            *package_names: Package names to scan (e.g., "tools.builtin", "plugins").

        Returns:
            List of newly discovered tool instances.
        """
        newly_registered: list[BaseTool] = []

        for package_name in package_names:
            try:
                package = importlib.import_module(package_name)
            except ImportError as e:
                logger.warning(f"Could not import package '{package_name}': {e}")
                continue

            package_path = Path(package.__file__).parent if package.__file__ else None
            if package_path is None:
                logger.warning(f"No file path for package '{package_name}'")
                continue

            for _, modname, is_pkg in pkgutil.iter_modules([str(package_path)]):
                full_modname = f"{package_name}.{modname}"
                try:
                    module = importlib.import_module(full_modname)
                except ImportError as e:
                    logger.warning(f"Skipping module '{full_modname}': {e}")
                    continue

                for name, obj in inspect.getmembers(module, inspect.isclass):
                    if not issubclass(obj, BaseTool):
                        continue
                    if obj is BaseTool:
                        continue
                    # Skip abstract classes
                    if inspect.isabstract(obj):
                        continue
                    # Skip classes from base module
                    if obj.__module__.startswith("langchain_core.tools"):
                        continue

                    try:
                        instance = obj()
                    except Exception as e:
                        logger.warning(f"Could not instantiate '{name}': {e}")
                        continue

                    if instance.name not in self._tools:
                        self._tools[instance.name] = instance
                        self.catalog.register(instance, plugin_source=full_modname)
                        newly_registered.append(instance)
                        logger.info(
                            f"Registered tool: '{instance.name}' "
                            f"(from {full_modname}.{name})"
                        )

        return newly_registered

    # ── Registration ──────────────────────────────────────

    def register(self, tool: BaseTool) -> None:
        """Manually register a tool instance."""
        self._tools[tool.name] = tool
        self.catalog.register(tool)
        logger.info(f"Manually registered tool: '{tool.name}'")

    def unregister(self, name: str) -> bool:
        """Remove a tool by name. Returns True if it existed."""
        if name in self._tools:
            del self._tools[name]
            self.catalog.unregister(name)
            return True
        return False

    def clear(self) -> None:
        """Remove all registered tools and catalog metadata."""
        self._tools.clear()
        self.catalog = ToolCatalog()

    # ── Access ────────────────────────────────────────────

    def get_all(self) -> list[BaseTool]:
        """Return all registered tools."""
        return list(self._tools.values())

    def get_for_state(self, state: AgentState) -> tuple[list[BaseTool], list[RegisteredToolSpec]]:
        """Return tools visible for the current AgentState and their specs."""
        return build_tool_pool(state, self.get_all(), self.catalog)

    def get_by_name(self, name: str) -> Optional[BaseTool]:
        """Get a specific tool by its name."""
        return self._tools.get(name)

    def get_spec(self, name: str) -> RegisteredToolSpec | None:
        """Return policy metadata for a registered tool."""
        return self.catalog.get(name)

    def get_specs(self) -> list[RegisteredToolSpec]:
        """Return metadata for all registered tools."""
        return self.catalog.get_all()

    def get_names(self) -> list[str]:
        """Return list of registered tool names."""
        return list(self._tools.keys())

    @property
    def count(self) -> int:
        return len(self._tools)

    # ── Info ──────────────────────────────────────────────

    def describe(self) -> str:
        """Human-readable description of all registered tools."""
        lines = [f"SkillRegistry: {self.count} tools registered"]
        for name, tool in self._tools.items():
            desc = tool.description[:80].replace("\n", " ")
            lines.append(f"  • {name}: {desc}...")
        return "\n".join(lines)


# ── Global singleton ────────────────────────────────────────

registry = SkillRegistry()
