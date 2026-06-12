"""Tool catalog, metadata normalization, and dynamic tool pool selection."""

from __future__ import annotations

import re
from typing import Any

from langchain_core.tools import BaseTool

from agent.state import AgentState, RegisteredToolSpec, TaskStep, ToolCapability
from safety.engine import SecurityPolicyEngine


PHASES = {"clarify", "observe", "diagnose", "propose", "approve", "execute", "verify", "report"}
POLICIES = {"no_tools", "read_only_tools", "write_tools_after_approval"}
RISK_ORDER = {"low": 1, "medium": 2, "high": 3, "critical": 4}
WRITE_TOOL_HINT_RE = re.compile(r"(write|execute|shell|delete|drop|alter|grant|revoke)", re.IGNORECASE)
POSTGRES_READ_ALIASES = {"postgres_read", "postgres_readonly", "postgres_query", "postgres_observe"}
POSTGRES_WRITE_ALIASES = {"postgres_write", "postgres_execute", "postgres_execute_write"}
POSTGRES_MUTATING_OPERATION_TYPES = {
    "data_change",
    "schema_change",
    "permission_change",
    "backup_restore",
    "maintenance",
}


def _schema_dict(tool: BaseTool) -> dict[str, Any]:
    schema = getattr(tool, "args_schema", None)
    if schema is None:
        return {}
    if hasattr(schema, "model_json_schema"):
        return schema.model_json_schema()
    if hasattr(schema, "schema"):
        return schema.schema()
    return {}


def _tool_attr(tool: BaseTool, name: str, default: Any) -> Any:
    return getattr(tool, name, default)


def default_capability_for_tool(tool: BaseTool) -> ToolCapability:
    """Infer conservative capability metadata for legacy tools."""
    name = tool.name
    description = getattr(tool, "description", "") or ""
    text = f"{name} {description}".lower()

    if "postgres" in text or "sql" in text or "database" in text:
        domain = "postgresql"
    elif "file" in text:
        domain = "filesystem"
    elif "shell" in text or name == "shell_execute":
        domain = "shell"
    elif "search" in text or "grep" in text:
        domain = "code"
    else:
        domain = "external"

    destructive = bool(WRITE_TOOL_HINT_RE.search(name))
    read_only = not destructive
    operation_type = "read_only" if read_only else "none"
    risk_level = "low"
    requires_approval = False

    if name == "file_write":
        operation_type = "documentation"
        read_only = False
        destructive = True
        risk_level = "medium"
    elif name == "shell_execute":
        operation_type = "none"
        read_only = False
        destructive = True
        risk_level = "high"
        requires_approval = True
    elif domain == "postgresql" and destructive:
        operation_type = "data_change"
        read_only = False
        risk_level = "high"
        requires_approval = True

    return {
        "domain": str(_tool_attr(tool, "tool_domain", domain)),
        "operation_type": str(_tool_attr(tool, "operation_type", operation_type)),
        "risk_level": str(_tool_attr(tool, "risk_level", risk_level)),
        "read_only": bool(_tool_attr(tool, "read_only", read_only)),
        "destructive": bool(_tool_attr(tool, "destructive", destructive)),
        "requires_approval": bool(_tool_attr(tool, "requires_approval", requires_approval)),
        "requires_transaction": bool(_tool_attr(tool, "requires_transaction", False)),
        "supports_parallel": bool(_tool_attr(tool, "supports_parallel", True)),
    }  # type: ignore[typeddict-item]


def build_tool_spec(tool: BaseTool, plugin_source: str | None = None) -> RegisteredToolSpec:
    capability = default_capability_for_tool(tool)
    allowed_phases = list(_tool_attr(tool, "allowed_phases", []))
    allowed_policies = list(_tool_attr(tool, "allowed_policies", []))

    if not allowed_policies:
        if capability["read_only"]:
            allowed_policies = ["read_only_tools"]
        elif capability["requires_approval"]:
            allowed_policies = ["write_tools_after_approval"]
        else:
            allowed_policies = ["read_only_tools", "write_tools_after_approval"]

    if not allowed_phases:
        if capability["read_only"]:
            allowed_phases = ["observe", "diagnose", "verify"]
        elif capability["requires_approval"]:
            allowed_phases = ["execute"]
        else:
            allowed_phases = ["execute"]

    return {
        "name": tool.name,
        "description": getattr(tool, "description", "") or "",
        "args_schema": _schema_dict(tool),
        "capability": capability,
        "allowed_phases": [phase for phase in allowed_phases if phase in PHASES],
        "allowed_policies": [policy for policy in allowed_policies if policy in POLICIES],
        "output_type": str(_tool_attr(tool, "output_type", "tool_result")),
        "result_sensitivity": str(_tool_attr(tool, "result_sensitivity", "internal")),
        "plugin_source": plugin_source,
        "enabled": True,
        "search_hint": _tool_attr(tool, "search_hint", None),
        "defer_loading": bool(_tool_attr(tool, "defer_loading", False)),
        "always_load": bool(_tool_attr(tool, "always_load", True)),
    }  # type: ignore[typeddict-item]


class ToolCatalog:
    """Metadata index for registered tools."""

    def __init__(self) -> None:
        self._specs: dict[str, RegisteredToolSpec] = {}

    def register(self, tool: BaseTool, plugin_source: str | None = None) -> RegisteredToolSpec:
        spec = build_tool_spec(tool, plugin_source)
        self._specs[spec["name"]] = spec
        return spec

    def unregister(self, name: str) -> bool:
        return self._specs.pop(name, None) is not None

    def get(self, name: str) -> RegisteredToolSpec | None:
        return self._specs.get(name)

    def get_all(self) -> list[RegisteredToolSpec]:
        return list(self._specs.values())

    def names(self) -> list[str]:
        return list(self._specs.keys())


def current_step(state: AgentState) -> TaskStep | None:
    steps = list(state.get("task_stack", []))
    step_id = state.get("current_step_id")
    if step_id:
        for step in steps:
            if step.get("id") == step_id:
                return step
    idx = state.get("current_task_index", 0)
    if steps and idx < len(steps):
        return steps[idx]
    return None


def _risk_at_most(actual: str, maximum: str) -> bool:
    return RISK_ORDER.get(actual, 99) <= RISK_ORDER.get(maximum, 0)


def _has_current_step_approval(state: AgentState, step_id: str | None) -> bool:
    if not step_id:
        return False
    return any(
        decision.get("step_id") == step_id and decision.get("status") == "approved"
        for decision in state.get("approval_decisions", [])
    )


def spec_allowed_for_state(spec: RegisteredToolSpec, state: AgentState) -> bool:
    if not spec.get("enabled", True):
        return False

    step = current_step(state)
    if not step:
        return True

    phase = str(step.get("phase", ""))
    policy = str(step.get("tool_policy", "no_tools"))
    expected_tools = set(step.get("expected_tools", []) or [])
    capability = spec["capability"]
    name = spec["name"]
    db_env = state.get("database_environment") or {}
    runtime_policy = state.get("runtime_policy") or {}

    if policy == "no_tools":
        return False
    is_mutating_postgres_tool = (
        capability["domain"] == "postgresql"
        and (capability["destructive"] or capability["operation_type"] in POSTGRES_MUTATING_OPERATION_TYPES)
    )
    if is_mutating_postgres_tool and (
        db_env.get("is_production") or runtime_policy.get("allow_database_writes") is False
    ):
        return False
    if phase and spec["allowed_phases"] and phase not in spec["allowed_phases"]:
        return False
    if policy and spec["allowed_policies"] and policy not in spec["allowed_policies"]:
        return False

    visibility = SecurityPolicyEngine(state).evaluate_tool_visibility(spec)
    if visibility["decision"] != "allow":
        return False

    if expected_tools and name not in expected_tools:
        if capability["domain"] == "postgresql" and capability["read_only"] and expected_tools & POSTGRES_READ_ALIASES:
            pass
        elif capability["domain"] == "postgresql" and not capability["read_only"] and expected_tools & POSTGRES_WRITE_ALIASES:
            pass
        else:
            if capability["domain"] == "postgresql" or capability["risk_level"] in {"high", "critical"}:
                return False

    if policy == "read_only_tools":
        return bool(capability["read_only"])

    if policy == "write_tools_after_approval":
        if capability["requires_approval"] and not _has_current_step_approval(state, step.get("id")):
            return False
        return _risk_at_most(capability["risk_level"], "critical")

    return True


def build_tool_pool(state: AgentState, tools: list[BaseTool], catalog: ToolCatalog) -> tuple[list[BaseTool], list[RegisteredToolSpec]]:
    """Return tools and specs visible to the model for the current state."""
    visible_tools: list[BaseTool] = []
    visible_specs: list[RegisteredToolSpec] = []
    for tool in tools:
        spec = catalog.get(tool.name)
        if spec is None:
            spec = catalog.register(tool)
        if spec_allowed_for_state(spec, state):
            visible_tools.append(tool)
            visible_specs.append(spec)
    return visible_tools, visible_specs
