"""Shared LLM factory — creates the appropriate LLM instance based on provider.

Supports:
  - DeepSeek (deepseek-chat, deepseek-reasoner) via ChatDeepSeek
  - OpenAI (gpt-4o, gpt-4-turbo, etc.) via ChatOpenAI

All graph nodes use this factory instead of creating LLM instances directly.
This ensures consistent configuration and single-point provider switching.
"""

from __future__ import annotations

import logging
from typing import Optional

from langchain_core.language_models import BaseChatModel

from agent.config import get_settings
from agent.state import AgentState, ModelInvocationRecord, ModelProfile, ModelRoute, ModelTask
from models.provider import provider_adapter_for
from models.routing import (
    ModelRouter,
    pending_invocation_record,
    profile_for_route,
)

logger = logging.getLogger(__name__)


def create_llm(
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
    timeout: Optional[float] = None,
    streaming: bool = False,
    model_id: Optional[str] = None,
    provider: Optional[str] = None,
) -> BaseChatModel:
    """Create an LLM instance based on the configured provider.

    Args:
        temperature: Override the default temperature (uses config value if None).
        max_tokens: Override the default max_tokens (uses config value if None).
        timeout: Override the default timeout (uses config node_timeout if None).
        streaming: Enable streaming mode for token-by-token output.

    Returns:
        A BaseChatModel instance (ChatDeepSeek or ChatOpenAI).
    """
    settings = get_settings()
    temp = temperature if temperature is not None else settings.llm_temperature
    tokens = max_tokens if max_tokens is not None else settings.llm_max_tokens
    timeout_val = timeout if timeout is not None else settings.node_timeout_seconds

    provider_name = provider or ("deepseek" if settings.is_deepseek else "openai")
    selected_model = model_id or settings.llm_model

    if provider_name == "deepseek":
        return _create_deepseek(temp, tokens, timeout_val, streaming, selected_model)
    if provider_name == "openai":
        return _create_openai(temp, tokens, timeout_val, streaming, selected_model)
    else:
        raise ValueError(f"Unsupported LLM provider: {provider_name}")


def create_llm_no_tools(
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
    timeout: Optional[float] = None,
) -> BaseChatModel:
    """Create an LLM without tool binding (for planning, summarization).

    Same as create_llm() but explicitly returns a model without tools bound.
    """
    return create_llm(
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout,
        streaming=False,
    )


def create_llm_with_tools(
    tools: list,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
    timeout: Optional[float] = None,
) -> BaseChatModel:
    """Create an LLM with tools bound for function calling.

    Args:
        tools: List of langchain BaseTool instances to bind.
        temperature: Override the default temperature.
        max_tokens: Override the default max_tokens.
        timeout: Override the default timeout.

    Returns:
        A BaseChatModel with bind_tools(tools) applied.
    """
    llm = create_llm(
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout,
        streaming=False,
    )
    if tools:
        llm = llm.bind_tools(tools)
        logger.debug(f"Bound {len(tools)} tools to LLM")
    return llm


def create_llm_for_task(
    task: ModelTask,
    state: AgentState | None = None,
    tools: list | None = None,
    structured_output_schema: str | None = None,
    preferred_model: str | None = None,
    delegated_task_id: str | None = None,
) -> tuple[BaseChatModel, ModelRoute, ModelInvocationRecord, ModelProfile | None]:
    """Route and create an LLM for a named model task.

    Returns the LLM plus route/record/profile metadata so callers can append
    model routing state after invocation succeeds or fails.
    """
    state = state or {}
    router = ModelRouter(state)
    route = router.route(
        task,
        tools=tools,
        structured_output_schema=structured_output_schema,
        delegated_task_id=delegated_task_id,
        preferred_model=preferred_model,
    )
    profile = profile_for_route(route, state.get("model_profiles") or None)
    if profile is None:
        raise ValueError(f"No model profile found for route model={route['selected_model_id']}")
    adapter = provider_adapter_for(route["provider"])
    validation_errors = adapter.validate_model(profile, route)
    if validation_errors:
        raise ValueError(f"Model route validation failed: {', '.join(validation_errors)}")
    policy = route["policy"]
    llm = adapter.create_chat_model(route, profile)
    if tools and policy["tools_allowed"]:
        llm = llm.bind_tools(tools)
        logger.debug(f"Bound {len(tools)} tools to routed LLM for task={task}")
    record = pending_invocation_record(
        route,
        state=state,
        structured_output_schema=structured_output_schema,
        delegated_task_id=delegated_task_id,
    )
    return llm, route, record, profile


# ── Private provider constructors ─────────────────────────────

def _create_deepseek(
    temperature: float,
    max_tokens: int,
    timeout: float,
    streaming: bool,
    model_id: Optional[str] = None,
) -> BaseChatModel:
    """Create a ChatDeepSeek instance."""
    from langchain_deepseek import ChatDeepSeek

    settings = get_settings()

    if not settings.deepseek_api_key:
        raise ValueError(
            "DEEPSEEK_API_KEY is required when LLM_PROVIDER=deepseek. "
            "Set it in your .env file."
        )

    logger.info(
        f"Creating DeepSeek LLM: model={model_id or settings.llm_model}, "
        f"temperature={temperature}, max_tokens={max_tokens}"
    )

    return ChatDeepSeek(
        model=model_id or settings.llm_model,
        api_key=settings.deepseek_api_key,
        api_base=settings.deepseek_base_url,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout,
        streaming=streaming,
    )


def _create_openai(
    temperature: float,
    max_tokens: int,
    timeout: float,
    streaming: bool,
    model_id: Optional[str] = None,
) -> BaseChatModel:
    """Create a ChatOpenAI instance."""
    from langchain_openai import ChatOpenAI

    settings = get_settings()

    if not settings.openai_api_key:
        raise ValueError(
            "OPENAI_API_KEY is required when LLM_PROVIDER=openai. "
            "Set it in your .env file."
        )

    logger.info(
        f"Creating OpenAI LLM: model={model_id or settings.llm_model}, "
        f"temperature={temperature}, max_tokens={max_tokens}"
    )

    return ChatOpenAI(
        model=model_id or settings.llm_model,
        api_key=settings.openai_api_key,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout,
        streaming=streaming,
    )
