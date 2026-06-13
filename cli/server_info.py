"""Helpers for reading safe server-side runtime metadata."""

from __future__ import annotations

from typing import Any

import httpx

from cli.config import CliRuntimeConfig


async def fetch_agent_info(config: CliRuntimeConfig) -> dict[str, Any] | None:
    """Return `/agent/info` metadata, or None when unavailable."""

    try:
        headers = {"X-API-Key": config.api_key} if config.api_key else {}
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{config.server_url}/agent/info", headers=headers)
            resp.raise_for_status()
            data = resp.json()
            return data if isinstance(data, dict) else None
    except Exception:
        return None
