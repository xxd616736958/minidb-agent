"""First-run and reconnect configuration prompts."""

from __future__ import annotations

import sys
from dataclasses import replace

from rich.prompt import Prompt

from cli.config import CliRuntimeConfig, normalize_target_environment, persist_runtime_defaults, save_user_config
from cli.display import console


def ensure_database_config(config: CliRuntimeConfig, *, interactive: bool = True) -> CliRuntimeConfig:
    """Prompt once for required PostgreSQL target settings when missing."""

    if config.database_url:
        persist_runtime_defaults(config)
        return config
    if not interactive or not sys.stdin.isatty():
        return config

    console.print("[yellow]PostgreSQL target is not configured.[/yellow]")
    database_url = Prompt.ask("PostgreSQL URL", password=True)
    target_env = Prompt.ask(
        "Environment",
        choices=["local", "dev", "staging", "production", "unknown"],
        default="dev",
    )
    new_config = replace(
        config,
        database_url=database_url.strip(),
        target_environment=normalize_target_environment(target_env),
    )
    persist_runtime_defaults(new_config)
    return new_config


def prompt_reconnect_config(config: CliRuntimeConfig) -> CliRuntimeConfig:
    """Prompt for a new PostgreSQL target and persist it."""

    database_url = Prompt.ask("New PostgreSQL URL", password=True)
    target_env = Prompt.ask(
        "Environment",
        choices=["local", "dev", "staging", "production", "unknown"],
        default=normalize_target_environment(config.target_environment),
    )
    new_config = replace(
        config,
        database_url=database_url.strip(),
        target_environment=normalize_target_environment(target_env),
    )
    save_user_config(
        {
            "database_url": new_config.database_url,
            "target_environment": new_config.target_environment,
            "approval_mode": new_config.approval_mode,
            "server_url": new_config.server_url,
        }
    )
    return new_config
