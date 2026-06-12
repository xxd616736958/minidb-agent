"""Provider adapters for routed chat model construction and validation."""

from __future__ import annotations

from typing import Protocol

from langchain_core.language_models import BaseChatModel

from agent.config import get_settings
from agent.state import ModelInvocationPolicy, ModelInvocationRecord, ModelProfile, ModelRoute


class ProviderAdapter(Protocol):
    """Uniform interface for provider-specific chat model construction."""

    provider: str

    def create_chat_model(self, route: ModelRoute, profile: ModelProfile) -> BaseChatModel:
        """Create a LangChain chat model for one resolved route."""

    def validate_model(self, profile: ModelProfile, route: ModelRoute) -> list[str]:
        """Return validation errors for a route/profile pair."""

    def estimate_cost(self, record: ModelInvocationRecord, profile: ModelProfile) -> float | None:
        """Estimate provider cost for an invocation record."""

    def normalize_error(self, error: Exception) -> dict[str, str]:
        """Normalize provider-specific errors for audit records."""


class BaseProviderAdapter:
    provider: str

    def validate_model(self, profile: ModelProfile, route: ModelRoute) -> list[str]:
        errors: list[str] = []
        if profile.get("provider") != route.get("provider"):
            errors.append("provider_mismatch")
        if profile.get("model_id") != route.get("selected_model_id"):
            errors.append("model_mismatch")
        if profile.get("deprecated"):
            errors.append("deprecated_model")
        for capability in route.get("required_capabilities", []):
            if not profile.get(capability, False):
                errors.append(f"missing_{capability}")
        if route.get("task") in profile.get("forbidden_tasks", []):
            errors.append("task_forbidden")
        return errors

    def estimate_cost(self, record: ModelInvocationRecord, profile: ModelProfile) -> float | None:
        cost_by_tier = {"cheap": 0.0002, "standard": 0.001, "premium": 0.003}
        total_tokens = int(record.get("input_tokens_estimate", 0)) + int(record.get("output_tokens_estimate", 0))
        return round((total_tokens / 1000) * cost_by_tier.get(profile.get("cost_tier", "standard"), 0.001), 6)

    def normalize_error(self, error: Exception) -> dict[str, str]:
        return {
            "error_type": type(error).__name__,
            "message": str(error),
        }

    @staticmethod
    def _policy(route: ModelRoute, profile: ModelProfile) -> ModelInvocationPolicy:
        policy = route["policy"]
        return {
            **policy,
            "max_tokens": min(int(policy["max_tokens"]), int(profile["max_output_tokens"])),
            "streaming": bool(policy["streaming"] and profile.get("supports_streaming")),
        }


class DeepSeekProviderAdapter(BaseProviderAdapter):
    provider = "deepseek"

    def create_chat_model(self, route: ModelRoute, profile: ModelProfile) -> BaseChatModel:
        from langchain_deepseek import ChatDeepSeek

        settings = get_settings()
        if not settings.deepseek_api_key:
            raise ValueError("DEEPSEEK_API_KEY is required when routing to DeepSeek.")
        policy = self._policy(route, profile)
        return ChatDeepSeek(
            model=profile["model_id"],
            api_key=settings.deepseek_api_key,
            api_base=settings.deepseek_base_url,
            temperature=policy["temperature"],
            max_tokens=policy["max_tokens"],
            timeout=policy["timeout_seconds"],
            streaming=policy["streaming"],
        )


class OpenAIProviderAdapter(BaseProviderAdapter):
    provider = "openai"

    def create_chat_model(self, route: ModelRoute, profile: ModelProfile) -> BaseChatModel:
        from langchain_openai import ChatOpenAI

        settings = get_settings()
        if not settings.openai_api_key:
            raise ValueError("OPENAI_API_KEY is required when routing to OpenAI.")
        policy = self._policy(route, profile)
        return ChatOpenAI(
            model=profile["model_id"],
            api_key=settings.openai_api_key,
            temperature=policy["temperature"],
            max_tokens=policy["max_tokens"],
            timeout=policy["timeout_seconds"],
            streaming=policy["streaming"],
        )


def provider_adapter_for(provider: str) -> ProviderAdapter:
    """Resolve a provider adapter by provider name."""
    if provider == "deepseek":
        return DeepSeekProviderAdapter()
    if provider == "openai":
        return OpenAIProviderAdapter()
    raise ValueError(f"Unsupported model provider: {provider}")
