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

logger = logging.getLogger(__name__)


def create_llm(
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
    timeout: Optional[float] = None,
    streaming: bool = False,
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

    if settings.is_deepseek:
        return _create_deepseek(temp, tokens, timeout_val, streaming)
    else:
        return _create_openai(temp, tokens, timeout_val, streaming)


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


# ── Private provider constructors ─────────────────────────────

def _create_deepseek(
    temperature: float,
    max_tokens: int,
    timeout: float,
    streaming: bool,
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
        f"Creating DeepSeek LLM: model={settings.llm_model}, "
        f"temperature={temperature}, max_tokens={max_tokens}"
    )

    return ChatDeepSeek(
        model=settings.llm_model,
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
        f"Creating OpenAI LLM: model={settings.llm_model}, "
        f"temperature={temperature}, max_tokens={max_tokens}"
    )

    return ChatOpenAI(
        model=settings.llm_model,
        api_key=settings.openai_api_key,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout,
        streaming=streaming,
    )
