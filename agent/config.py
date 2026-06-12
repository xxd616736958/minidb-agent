"""Agent configuration loaded from environment variables.

Uses pydantic-settings for validation and auto-loading from .env file.
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class AgentSettings(BaseSettings):
    """All agent configuration, sourced from environment / .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── LLM Provider ────────────────────────────────────
    llm_provider: str = Field(
        default="deepseek", alias="LLM_PROVIDER",
        description="LLM provider: 'openai' or 'deepseek'"
    )
    openai_api_key: str = Field(
        default="", alias="OPENAI_API_KEY",
        description="OpenAI API key (used when LLM_PROVIDER=openai)"
    )
    deepseek_api_key: str = Field(
        default="", alias="DEEPSEEK_API_KEY",
        description="DeepSeek API key (used when LLM_PROVIDER=deepseek)"
    )
    deepseek_base_url: str = Field(
        default="https://api.deepseek.com", alias="DEEPSEEK_BASE_URL",
        description="DeepSeek API base URL"
    )
    llm_model: str = Field(
        default="deepseek-chat", alias="LLM_MODEL",
        description="Model identifier (deepseek-chat, deepseek-reasoner, gpt-4o, etc.)"
    )
    llm_temperature: float = Field(
        default=0.0, alias="LLM_TEMPERATURE",
        description="LLM sampling temperature"
    )
    llm_max_tokens: int = Field(
        default=4096, alias="LLM_MAX_TOKENS",
        description="Max output tokens per LLM call"
    )
    model_routing_enabled: bool = Field(
        default=True, alias="MODEL_ROUTING_ENABLED",
        description="Enable task-aware model routing"
    )
    model_allowlist: str = Field(
        default="deepseek-chat,deepseek-reasoner,gpt-4o,gpt-4o-mini",
        alias="MODEL_ALLOWLIST",
        description="Comma-separated list of model IDs allowed for routing"
    )
    default_model_alias: str = Field(
        default="balanced", alias="DEFAULT_MODEL_ALIAS",
        description="Default model alias for general tasks"
    )
    planning_model_alias: str = Field(
        default="balanced", alias="PLANNING_MODEL_ALIAS",
        description="Model alias for planning tasks"
    )
    tool_reasoning_model_alias: str = Field(
        default="balanced", alias="TOOL_REASONING_MODEL_ALIAS",
        description="Model alias for tool reasoning tasks"
    )
    safety_review_model_alias: str = Field(
        default="review", alias="SAFETY_REVIEW_MODEL_ALIAS",
        description="Model alias for high-risk safety review tasks"
    )
    report_model_alias: str = Field(
        default="cheap", alias="REPORT_MODEL_ALIAS",
        description="Model alias for report generation"
    )
    memory_model_alias: str = Field(
        default="cheap", alias="MEMORY_MODEL_ALIAS",
        description="Model alias for memory compaction"
    )
    model_forbid_silent_downshift_for_high_risk: bool = Field(
        default=True,
        alias="MODEL_FORBID_SILENT_DOWNSHIFT_FOR_HIGH_RISK",
        description="Disallow silent model downshift for high-risk database tasks"
    )
    model_max_context_safety_ratio: float = Field(
        default=0.8,
        alias="MODEL_MAX_CONTEXT_SAFETY_RATIO",
        description="Fraction of model context window considered safe for routing"
    )

    # ── LangSmith ──────────────────────────────────────
    langsmith_tracing: bool = Field(
        default=True, alias="LANGSMITH_TRACING",
    )
    langsmith_api_key: str = Field(
        default="", alias="LANGSMITH_API_KEY",
    )
    langsmith_project: str = Field(
        default="zuixiaoagent", alias="LANGSMITH_PROJECT",
    )
    langsmith_endpoint: str = Field(
        default="https://api.smith.langchain.com", alias="LANGSMITH_ENDPOINT",
    )

    # ── Database ───────────────────────────────────────
    postgres_uri: str = Field(
        default="", alias="POSTGRES_URI",
        description="PostgreSQL connection string; if empty, SQLite is used"
    )

    # ── Agent ──────────────────────────────────────────
    max_retries: int = Field(
        default=3, alias="MAX_RETRIES",
        description="Max retry attempts per node failure"
    )
    node_timeout_seconds: float = Field(
        default=60.0, alias="NODE_TIMEOUT_SECONDS",
        description="Per-node timeout in seconds"
    )
    memory_window_tokens: int = Field(
        default=8000, alias="MEMORY_WINDOW_TOKENS",
        description="Short-term memory max tokens before compaction"
    )
    memory_compact_threshold: int = Field(
        default=12000, alias="MEMORY_COMPACT_THRESHOLD",
        description="Token count threshold to trigger compaction"
    )
    memory_store_path: str = Field(
        default="data/memory_records.json",
        alias="MEMORY_STORE_PATH",
        description="Path for PostgreSQL agent long-term memory records"
    )

    # ── Shell Tool ─────────────────────────────────────
    command_whitelist: str = Field(
        default="ls,cat,grep,find,pwd,echo,python3,python,pip3,pip,node,npm,"
                "git,curl,wget,head,tail,wc,sort,mkdir,touch,cp,mv,"
                "ps,kill,docker,kubectl,make,cargo,go,rustc,tsc",
        alias="COMMAND_WHITELIST",
        description="Comma-separated list of allowed shell commands"
    )
    dangerous_commands: str = Field(
        default="rm,dd,mkfs,shutdown,reboot,sudo,su",
        alias="DANGEROUS_COMMANDS",
        description="Comma-separated list of commands requiring human approval"
    )

    # ── Server Auth ────────────────────────────────────
    agent_api_key: str = Field(
        default="", alias="AGENT_API_KEY",
        description="API key for server authentication (empty = no auth)"
    )

    # ── Logging ────────────────────────────────────────
    agent_log_level: str = Field(
        default="INFO", alias="AGENT_LOG_LEVEL",
    )

    # ── Derived properties ─────────────────────────────
    @property
    def use_postgres(self) -> bool:
        """True if PostgreSQL is configured, False for SQLite fallback."""
        return bool(self.postgres_uri)

    @property
    def is_deepseek(self) -> bool:
        """True if DeepSeek is the active LLM provider."""
        return self.llm_provider == "deepseek"

    @property
    def active_api_key(self) -> str:
        """Return the API key for the active LLM provider."""
        if self.is_deepseek:
            return self.deepseek_api_key
        return self.openai_api_key

    @property
    def command_whitelist_set(self) -> set[str]:
        """Parsed set of allowed commands."""
        return {c.strip() for c in self.command_whitelist.split(",") if c.strip()}

    @property
    def dangerous_commands_set(self) -> set[str]:
        """Parsed set of dangerous commands."""
        return {c.strip() for c in self.dangerous_commands.split(",") if c.strip()}

    @property
    def model_allowlist_set(self) -> set[str]:
        """Parsed set of allowed model IDs."""
        return {c.strip() for c in self.model_allowlist.split(",") if c.strip()}

    @property
    def auth_enabled(self) -> bool:
        """True if API key authentication is configured."""
        return bool(self.agent_api_key)


@lru_cache()
def get_settings() -> AgentSettings:
    """Return the cached singleton settings instance."""
    return AgentSettings()
