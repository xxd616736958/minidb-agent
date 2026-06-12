"""Model capability registry, task routing, and invocation audit records."""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from typing import Any

from agent.config import get_settings
from agent.context import estimate_tokens
from agent.state import (
    AgentState,
    ModelEvaluationResult,
    ModelFallbackDecision,
    ModelInvocationPolicy,
    ModelInvocationRecord,
    ModelProfile,
    ModelRoute,
    ModelTask,
)


MODEL_TASKS: set[str] = {
    "intent_understanding",
    "planning",
    "tool_reasoning",
    "sql_safety_review",
    "delegation_worker",
    "delegation_reviewer",
    "error_recovery",
    "memory_compaction",
    "report_generation",
    "quality_evaluation",
}
RISK_RANK = {"unknown": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}
SENSITIVITY_RANK = {"public": 1, "internal": 2, "sensitive": 3, "secret": 4}
QUALITY_RANK = {"fast": 1, "balanced": 2, "strong": 3, "review": 4}
DEFAULT_COST_BY_TIER = {"cheap": 0.0002, "standard": 0.001, "premium": 0.003}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def current_risk_level(state: AgentState) -> str:
    step = {}
    step_id = state.get("current_step_id")
    for candidate in state.get("task_stack", []) or []:
        if step_id and candidate.get("id") == step_id:
            step = candidate
            break
    if not step and state.get("task_stack"):
        steps = state.get("task_stack", []) or []
        index = int(state.get("current_task_index") or 0)
        if 0 <= index < len(steps):
            step = steps[index]
    plan = state.get("db_task_plan") or {}
    intent = state.get("current_intent") or {}
    return str(step.get("risk_level") or plan.get("global_risk_level") or intent.get("risk_level") or "unknown")


def is_high_risk(state: AgentState) -> bool:
    risk = current_risk_level(state)
    db_env = state.get("database_environment") or {}
    intent = state.get("current_intent") or {}
    return (
        RISK_RANK.get(risk, 0) >= RISK_RANK["high"]
        or db_env.get("is_production") is True
        or intent.get("target_environment") == "production"
    )


def context_token_estimate(state: AgentState) -> int:
    chunks = [
        str((state.get("current_intent") or {}).get("goal") or ""),
        str((state.get("db_task_plan") or {}).get("summary") or ""),
        " ".join(str(item.get("summary", "")) for item in state.get("db_observations", [])[-20:]),
        " ".join(str(item.get("summary", "")) for item in state.get("retrieved_memories", [])[-10:]),
    ]
    return estimate_tokens("\n".join(chunks))


def data_sensitivity_for_state(state: AgentState) -> str:
    output_policy = state.get("output_safety_policy") or {}
    if output_policy.get("allow_raw_result_in_memory") is False:
        return "sensitive"
    artifacts = state.get("artifact_records", []) or []
    sensitivities = [str(item.get("sensitivity") or "internal") for item in artifacts]
    if "secret" in sensitivities:
        return "secret"
    if "sensitive" in sensitivities:
        return "sensitive"
    return "internal"


def default_model_profiles() -> list[ModelProfile]:
    """Built-in model profiles for the first-phase model router."""
    settings = get_settings()
    active_provider = "deepseek" if settings.is_deepseek else "openai"
    active_model = settings.llm_model
    profiles: list[ModelProfile] = [
        {
            "id": "model-deepseek-chat",
            "provider": "deepseek",
            "model_id": "deepseek-chat",
            "aliases": ["cheap", "balanced", "deepseek-chat"],
            "display_name": "DeepSeek Chat",
            "description": "General low-cost model for task understanding, planning, reports, and tool reasoning.",
            "context_window_tokens": 64000,
            "max_output_tokens": 8192,
            "supports_tools": True,
            "supports_structured_output": True,
            "supports_streaming": True,
            "supports_reasoning_effort": False,
            "supports_parallel_tool_calls": False,
            "supports_long_context": False,
            "cost_tier": "cheap",
            "quality_tier": "balanced",
            "allowed_tasks": sorted(MODEL_TASKS - {"sql_safety_review"}),
            "forbidden_tasks": [],
            "allowed_data_sensitivity": "sensitive",
            "deprecated": False,
        },
        {
            "id": "model-deepseek-reasoner",
            "provider": "deepseek",
            "model_id": "deepseek-reasoner",
            "aliases": ["strong", "review", "deepseek-reasoner"],
            "display_name": "DeepSeek Reasoner",
            "description": "Reasoning-oriented model for high-risk planning, SQL safety review, and reviewer subagents.",
            "context_window_tokens": 64000,
            "max_output_tokens": 8192,
            "supports_tools": False,
            "supports_structured_output": True,
            "supports_streaming": True,
            "supports_reasoning_effort": False,
            "supports_parallel_tool_calls": False,
            "supports_long_context": False,
            "cost_tier": "standard",
            "quality_tier": "review",
            "allowed_tasks": sorted(MODEL_TASKS - {"tool_reasoning"}),
            "forbidden_tasks": ["tool_reasoning"],
            "allowed_data_sensitivity": "sensitive",
            "deprecated": False,
        },
        {
            "id": "model-gpt-4o-mini",
            "provider": "openai",
            "model_id": "gpt-4o-mini",
            "aliases": ["cheap", "fast", "gpt-4o-mini"],
            "display_name": "GPT-4o mini",
            "description": "Low-cost model for summaries, reports, and simple routing tasks.",
            "context_window_tokens": 128000,
            "max_output_tokens": 8192,
            "supports_tools": True,
            "supports_structured_output": True,
            "supports_streaming": True,
            "supports_reasoning_effort": False,
            "supports_parallel_tool_calls": True,
            "supports_long_context": True,
            "cost_tier": "cheap",
            "quality_tier": "fast",
            "allowed_tasks": sorted(MODEL_TASKS - {"sql_safety_review"}),
            "forbidden_tasks": [],
            "allowed_data_sensitivity": "sensitive",
            "deprecated": False,
        },
        {
            "id": "model-gpt-4o",
            "provider": "openai",
            "model_id": "gpt-4o",
            "aliases": ["balanced", "strong", "review", "gpt-4o"],
            "display_name": "GPT-4o",
            "description": "General strong model for tool reasoning, planning, safety review, and long context tasks.",
            "context_window_tokens": 128000,
            "max_output_tokens": 16384,
            "supports_tools": True,
            "supports_structured_output": True,
            "supports_streaming": True,
            "supports_reasoning_effort": False,
            "supports_parallel_tool_calls": True,
            "supports_long_context": True,
            "cost_tier": "premium",
            "quality_tier": "review",
            "allowed_tasks": sorted(MODEL_TASKS),
            "forbidden_tasks": [],
            "allowed_data_sensitivity": "sensitive",
            "deprecated": False,
        },
    ]
    if not any(profile["provider"] == active_provider and profile["model_id"] == active_model for profile in profiles):
        profiles.append(
            {
                "id": f"model-{active_provider}-{active_model}".replace("/", "-"),
                "provider": active_provider,  # type: ignore[typeddict-item]
                "model_id": active_model,
                "aliases": ["configured", active_model],
                "display_name": active_model,
                "description": "Model configured by LLM_PROVIDER/LLM_MODEL.",
                "context_window_tokens": 64000,
                "max_output_tokens": settings.llm_max_tokens,
                "supports_tools": True,
                "supports_structured_output": True,
                "supports_streaming": True,
                "supports_reasoning_effort": False,
                "supports_parallel_tool_calls": False,
                "supports_long_context": False,
                "cost_tier": "standard",
                "quality_tier": "balanced",
                "allowed_tasks": sorted(MODEL_TASKS),
                "forbidden_tasks": [],
                "allowed_data_sensitivity": "sensitive",
                "deprecated": False,
            }
        )
    allowlist = settings.model_allowlist_set
    if allowlist:
        profiles = [profile for profile in profiles if profile["model_id"] in allowlist]
    return profiles


def default_policy_for_task(task: ModelTask, state: AgentState | None = None) -> ModelInvocationPolicy:
    """Return deterministic per-task model invocation defaults."""
    settings = get_settings()
    high_risk = is_high_risk(state or {})
    policies: dict[str, ModelInvocationPolicy] = {
        "intent_understanding": {
            "task": task,
            "temperature": 0.0,
            "max_tokens": 1200,
            "timeout_seconds": settings.node_timeout_seconds,
            "reasoning_effort": None,
            "streaming": False,
            "tools_allowed": False,
            "structured_output_required": True,
            "allow_fallback": True,
            "allow_downshift": True,
            "require_review_model": False,
        },
        "planning": {
            "task": task,
            "temperature": 0.0,
            "max_tokens": 1024,
            "timeout_seconds": settings.node_timeout_seconds,
            "reasoning_effort": "medium" if high_risk else None,
            "streaming": False,
            "tools_allowed": False,
            "structured_output_required": True,
            "allow_fallback": True,
            "allow_downshift": not high_risk,
            "require_review_model": high_risk,
        },
        "tool_reasoning": {
            "task": task,
            "temperature": 0.0,
            "max_tokens": settings.llm_max_tokens,
            "timeout_seconds": settings.node_timeout_seconds,
            "reasoning_effort": None,
            "streaming": False,
            "tools_allowed": True,
            "structured_output_required": False,
            "allow_fallback": True,
            "allow_downshift": False,
            "require_review_model": False,
        },
        "sql_safety_review": {
            "task": task,
            "temperature": 0.0,
            "max_tokens": 1600,
            "timeout_seconds": settings.node_timeout_seconds,
            "reasoning_effort": "high",
            "streaming": False,
            "tools_allowed": False,
            "structured_output_required": True,
            "allow_fallback": True,
            "allow_downshift": False,
            "require_review_model": True,
        },
        "delegation_worker": {
            "task": task,
            "temperature": 0.0,
            "max_tokens": 1600,
            "timeout_seconds": settings.node_timeout_seconds,
            "reasoning_effort": None,
            "streaming": False,
            "tools_allowed": True,
            "structured_output_required": True,
            "allow_fallback": True,
            "allow_downshift": not high_risk,
            "require_review_model": high_risk,
        },
        "delegation_reviewer": {
            "task": task,
            "temperature": 0.0,
            "max_tokens": 1600,
            "timeout_seconds": settings.node_timeout_seconds,
            "reasoning_effort": "high",
            "streaming": False,
            "tools_allowed": False,
            "structured_output_required": True,
            "allow_fallback": True,
            "allow_downshift": False,
            "require_review_model": True,
        },
        "error_recovery": {
            "task": task,
            "temperature": 0.0,
            "max_tokens": 1200,
            "timeout_seconds": settings.node_timeout_seconds,
            "reasoning_effort": "medium" if high_risk else None,
            "streaming": False,
            "tools_allowed": False,
            "structured_output_required": True,
            "allow_fallback": True,
            "allow_downshift": not high_risk,
            "require_review_model": high_risk,
        },
        "memory_compaction": {
            "task": task,
            "temperature": 0.0,
            "max_tokens": 800,
            "timeout_seconds": settings.node_timeout_seconds,
            "reasoning_effort": None,
            "streaming": False,
            "tools_allowed": False,
            "structured_output_required": False,
            "allow_fallback": True,
            "allow_downshift": True,
            "require_review_model": False,
        },
        "report_generation": {
            "task": task,
            "temperature": 0.2,
            "max_tokens": 2400,
            "timeout_seconds": settings.node_timeout_seconds,
            "reasoning_effort": None,
            "streaming": False,
            "tools_allowed": False,
            "structured_output_required": False,
            "allow_fallback": True,
            "allow_downshift": True,
            "require_review_model": False,
        },
        "quality_evaluation": {
            "task": task,
            "temperature": 0.0,
            "max_tokens": 1200,
            "timeout_seconds": settings.node_timeout_seconds,
            "reasoning_effort": "medium" if high_risk else None,
            "streaming": False,
            "tools_allowed": False,
            "structured_output_required": True,
            "allow_fallback": True,
            "allow_downshift": not high_risk,
            "require_review_model": high_risk,
        },
    }
    return policies[task]


class ModelRegistry:
    """Resolve model aliases and validate capability constraints."""

    def __init__(self, profiles: list[ModelProfile] | None = None) -> None:
        self.profiles = profiles or default_model_profiles()

    def get(self, model_id_or_alias: str, provider: str | None = None) -> ModelProfile | None:
        for profile in self.profiles:
            if provider and profile["provider"] != provider:
                continue
            if profile["model_id"] == model_id_or_alias or model_id_or_alias in profile.get("aliases", []):
                return profile
        return None

    def candidates(
        self,
        *,
        task: ModelTask,
        required_capabilities: list[str],
        data_sensitivity: str,
        provider: str | None = None,
    ) -> list[ModelProfile]:
        candidates = []
        for profile in self.profiles:
            if provider and profile["provider"] != provider:
                continue
            if profile.get("deprecated"):
                continue
            if task not in profile.get("allowed_tasks", []):
                continue
            if task in profile.get("forbidden_tasks", []):
                continue
            if SENSITIVITY_RANK.get(data_sensitivity, 2) > SENSITIVITY_RANK.get(profile.get("allowed_data_sensitivity", "internal"), 2):
                continue
            if any(not profile.get(capability, False) for capability in required_capabilities):
                continue
            candidates.append(profile)
        return candidates

    @staticmethod
    def quality_at_least(profile: ModelProfile, tier: str) -> bool:
        return QUALITY_RANK.get(profile["quality_tier"], 0) >= QUALITY_RANK.get(tier, 0)


class ModelRouter:
    """Route model tasks to concrete model profiles and invocation policies."""

    def __init__(self, state: AgentState | None = None, registry: ModelRegistry | None = None) -> None:
        self.state = state or {}
        profiles = self.state.get("model_profiles") or None
        self.registry = registry or ModelRegistry(profiles)

    def route(
        self,
        task: ModelTask,
        *,
        tools: list[Any] | None = None,
        structured_output_schema: str | None = None,
        delegated_task_id: str | None = None,
        preferred_model: str | None = None,
    ) -> ModelRoute:
        policy = default_policy_for_task(task, self.state)
        settings = get_settings()
        preferred_model = (
            preferred_model
            or self._preferred_model_from_delegation(delegated_task_id)
            or (settings.llm_model if not settings.model_routing_enabled else None)
            or self.preferred_alias_for_task(task)
        )
        required = self.required_capabilities(policy, tools=tools, structured_output_schema=structured_output_schema)
        risk = current_risk_level(self.state)
        token_estimate = context_token_estimate(self.state)
        data_sensitivity = data_sensitivity_for_state(self.state)
        provider = "deepseek" if settings.is_deepseek else "openai"
        selected = self._select_profile(
            task=task,
            policy=policy,
            required_capabilities=required,
            data_sensitivity=data_sensitivity,
            provider=provider,
            token_estimate=token_estimate,
            preferred_model=preferred_model,
        )
        tools_bound = [str(getattr(tool, "name", tool)) for tool in (tools or [])] if policy["tools_allowed"] else []
        return {
            "id": new_id("model-route"),
            "task": task,
            "selected_model_id": selected["model_id"],
            "provider": selected["provider"],
            "reason": self._route_reason(task, selected, policy, required, token_estimate, delegated_task_id),
            "required_capabilities": required,
            "risk_level": risk,
            "context_tokens_estimate": token_estimate,
            "tools_bound": tools_bound,
            "policy": policy,
            "fallback_chain": self._fallback_chain(selected, task, required, data_sensitivity, provider),
            "created_at": now_iso(),
        }

    def _preferred_model_from_delegation(self, delegated_task_id: str | None) -> str | None:
        if not delegated_task_id:
            return None
        delegated_task = next(
            (item for item in self.state.get("delegated_tasks", []) if item.get("id") == delegated_task_id),
            None,
        )
        if not delegated_task:
            return None
        role_name = delegated_task.get("agent_role")
        role = next(
            (item for item in self.state.get("agent_roles", []) if item.get("name") == role_name),
            None,
        )
        default_model = str((role or {}).get("default_model") or "").strip()
        if not default_model or default_model == "inherit":
            return None
        if default_model == "strong_review":
            return get_settings().safety_review_model_alias
        return default_model

    @staticmethod
    def preferred_alias_for_task(task: ModelTask) -> str | None:
        settings = get_settings()
        mapping = {
            "intent_understanding": settings.default_model_alias,
            "planning": settings.planning_model_alias,
            "tool_reasoning": settings.tool_reasoning_model_alias,
            "sql_safety_review": settings.safety_review_model_alias,
            "delegation_worker": settings.default_model_alias,
            "delegation_reviewer": settings.safety_review_model_alias,
            "error_recovery": settings.planning_model_alias,
            "memory_compaction": settings.memory_model_alias,
            "report_generation": settings.report_model_alias,
            "quality_evaluation": settings.safety_review_model_alias,
        }
        return mapping.get(task)

    def required_capabilities(
        self,
        policy: ModelInvocationPolicy,
        *,
        tools: list[Any] | None = None,
        structured_output_schema: str | None = None,
    ) -> list[str]:
        required: list[str] = []
        if policy["tools_allowed"] and tools:
            required.append("supports_tools")
        if policy["structured_output_required"] or structured_output_schema:
            required.append("supports_structured_output")
        return required

    def _select_profile(
        self,
        *,
        task: ModelTask,
        policy: ModelInvocationPolicy,
        required_capabilities: list[str],
        data_sensitivity: str,
        provider: str,
        token_estimate: int,
        preferred_model: str | None,
    ) -> ModelProfile:
        preferred = self.registry.get(preferred_model, provider=provider) if preferred_model else None
        if preferred_model and not preferred:
            raise ValueError(f"Preferred model or alias '{preferred_model}' is not allowed for provider={provider}")
        if preferred and self._profile_allowed(preferred, task, policy, required_capabilities, data_sensitivity, token_estimate):
            return preferred

        settings = get_settings()
        configured = self.registry.get(settings.llm_model, provider=provider)
        if not preferred_model and configured and configured["provider"] == provider and self._profile_allowed(configured, task, policy, required_capabilities, data_sensitivity, token_estimate):
            if not policy["require_review_model"] or self.registry.quality_at_least(configured, "review"):
                return configured

        candidates = self.registry.candidates(
            task=task,
            required_capabilities=required_capabilities,
            data_sensitivity=data_sensitivity,
            provider=provider,
        )
        candidates = [
            profile for profile in candidates
            if token_estimate < int(profile["context_window_tokens"] * settings.model_max_context_safety_ratio)
        ] or candidates
        if policy["require_review_model"]:
            review_candidates = [profile for profile in candidates if self.registry.quality_at_least(profile, "review")]
            if review_candidates:
                candidates = review_candidates
        if not candidates:
            raise ValueError(f"No model profile can satisfy task={task}, capabilities={required_capabilities}")
        return sorted(
            candidates,
            key=lambda profile: (
                QUALITY_RANK.get(profile["quality_tier"], 0),
                -DEFAULT_COST_BY_TIER.get(profile["cost_tier"], 0.001),
            ),
            reverse=True,
        )[0]

    def _profile_allowed(
        self,
        profile: ModelProfile,
        task: ModelTask,
        policy: ModelInvocationPolicy,
        required_capabilities: list[str],
        data_sensitivity: str,
        token_estimate: int,
    ) -> bool:
        if profile.get("deprecated"):
            return False
        if task not in profile.get("allowed_tasks", []) or task in profile.get("forbidden_tasks", []):
            return False
        if any(not profile.get(capability, False) for capability in required_capabilities):
            return False
        if policy["require_review_model"] and not self.registry.quality_at_least(profile, "review"):
            return False
        if SENSITIVITY_RANK.get(data_sensitivity, 2) > SENSITIVITY_RANK.get(profile.get("allowed_data_sensitivity", "internal"), 2):
            return False
        return token_estimate < int(profile["context_window_tokens"] * get_settings().model_max_context_safety_ratio)

    def _fallback_chain(
        self,
        selected: ModelProfile,
        task: ModelTask,
        required_capabilities: list[str],
        data_sensitivity: str,
        provider: str,
    ) -> list[str]:
        candidates = self.registry.candidates(
            task=task,
            required_capabilities=required_capabilities,
            data_sensitivity=data_sensitivity,
            provider=provider,
        )
        return [
            profile["model_id"] for profile in candidates
            if profile["model_id"] != selected["model_id"]
        ][:3]

    @staticmethod
    def _route_reason(
        task: ModelTask,
        selected: ModelProfile,
        policy: ModelInvocationPolicy,
        required: list[str],
        token_estimate: int,
        delegated_task_id: str | None,
    ) -> str:
        parts = [
            f"Selected {selected['model_id']} for {task}",
            f"quality={selected['quality_tier']}",
            f"cost={selected['cost_tier']}",
        ]
        if policy["require_review_model"]:
            parts.append("review model required")
        if required:
            parts.append(f"requires {', '.join(required)}")
        if delegated_task_id:
            parts.append(f"delegated_task={delegated_task_id}")
        parts.append(f"context_estimate={token_estimate}")
        return "; ".join(parts)


def profile_for_route(route: ModelRoute, profiles: list[ModelProfile] | None = None) -> ModelProfile | None:
    registry = ModelRegistry(profiles)
    return registry.get(route["selected_model_id"])


def pending_invocation_record(
    route: ModelRoute,
    *,
    state: AgentState | None = None,
    structured_output_schema: str | None = None,
    input_tokens_estimate: int | None = None,
    delegated_task_id: str | None = None,
    quality_gate_id: str | None = None,
) -> ModelInvocationRecord:
    state = state or {}
    return {
        "id": new_id("model-call"),
        "route_id": route["id"],
        "task": route["task"],
        "provider": route["provider"],
        "model_id": route["selected_model_id"],
        "step_id": state.get("current_step_id"),
        "delegated_task_id": delegated_task_id,
        "quality_gate_id": quality_gate_id,
        "input_tokens_estimate": input_tokens_estimate if input_tokens_estimate is not None else route.get("context_tokens_estimate", 0),
        "output_tokens_estimate": 0,
        "duration_ms": None,
        "tools_bound": route.get("tools_bound", []),
        "structured_output_schema": structured_output_schema,
        "status": "pending",
        "error_type": None,
        "fallback_from": None,
        "cost_estimate": None,
        "created_at": now_iso(),
    }


def finish_invocation_record(
    record: ModelInvocationRecord,
    *,
    status: str,
    started_at: float,
    output_text: str = "",
    error: Exception | None = None,
    profile: ModelProfile | None = None,
) -> ModelInvocationRecord:
    output_tokens = estimate_tokens(output_text) if output_text else 0
    cost_rate = DEFAULT_COST_BY_TIER.get((profile or {}).get("cost_tier", "standard"), 0.001)
    total_tokens = int(record.get("input_tokens_estimate", 0)) + output_tokens
    return {
        **record,
        "status": status,  # type: ignore[typeddict-item]
        "output_tokens_estimate": output_tokens,
        "duration_ms": int((time.monotonic() - started_at) * 1000),
        "error_type": type(error).__name__ if error else None,
        "cost_estimate": round((total_tokens / 1000) * cost_rate, 6),
    }


def fallback_decision_for_error(
    route: ModelRoute,
    record: ModelInvocationRecord,
    error: Exception,
) -> ModelFallbackDecision:
    policy = route["policy"]
    high_risk = policy["require_review_model"] or RISK_RANK.get(route.get("risk_level", "unknown"), 0) >= RISK_RANK["high"]
    if high_risk and not policy["allow_downshift"]:
        decision = "fail_closed"
        to_model = None
        allowed = True
        reason = "High-risk model invocation failed; silent downshift is forbidden."
    elif policy["allow_fallback"] and route.get("fallback_chain"):
        decision = "downshift" if policy["allow_downshift"] else "upgrade"
        to_model = route["fallback_chain"][0]
        allowed = True
        reason = f"Fallback allowed after {type(error).__name__}."
    else:
        decision = "retry_same_model"
        to_model = route["selected_model_id"]
        allowed = policy["allow_fallback"]
        reason = f"No fallback model available after {type(error).__name__}."
    return {
        "id": new_id("model-fallback"),
        "invocation_id": record["id"],
        "from_model_id": route["selected_model_id"],
        "to_model_id": to_model,
        "decision": decision,  # type: ignore[typeddict-item]
        "reason": reason,
        "allowed_by_policy": allowed,
        "created_at": now_iso(),
    }


def evaluate_model_invocation(record: ModelInvocationRecord, case_id: str = "runtime") -> ModelEvaluationResult:
    """Build a small deterministic model evaluation from one invocation record."""
    failed_modes: list[str] = []
    scores = {
        "availability": 1.0 if record.get("status") in {"succeeded", "fallback_used"} else 0.0,
        "latency": 1.0 if (record.get("duration_ms") or 0) <= 60000 else 0.5,
        "tool_binding": 1.0 if record.get("task") != "tool_reasoning" or record.get("tools_bound") else 0.0,
    }
    if scores["availability"] == 0.0:
        failed_modes.append("model_invocation_failed")
    if scores["tool_binding"] == 0.0:
        failed_modes.append("tool_reasoning_without_bound_tools")
    status = "failed" if failed_modes else "passed"
    return {
        "id": new_id("model-eval"),
        "model_id": record["model_id"],
        "task": record["task"],
        "case_id": case_id,
        "status": status,  # type: ignore[typeddict-item]
        "scores": scores,
        "failure_modes": failed_modes,
        "safety_notes": [],
        "cost_estimate": record.get("cost_estimate"),
        "latency_ms": record.get("duration_ms"),
        "created_at": now_iso(),
    }
