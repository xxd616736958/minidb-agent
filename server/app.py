"""Custom FastAPI application for the LangGraph Server.

This app is mounted by langgraph serve (configured in langgraph.json http.app).
It adds:
  - API key authentication middleware
  - Health check endpoints
  - CORS headers for web UI access
  - Custom error handlers
"""

from __future__ import annotations

import logging
import os
import platform
import sys
from datetime import datetime

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from server.auth import APIKeyMiddleware

logger = logging.getLogger(__name__)

# ── App factory ──────────────────────────────────────────────

def create_app() -> FastAPI:
    """Create the FastAPI application with all middleware."""
    app = FastAPI(
        title="zuixiaoagent — Terminal Operating Agent",
        description=(
            "LangGraph-based terminal-operating programming intelligent agent. "
            "Provides HTTP API for agent interaction with shell execution, "
            "file operations, code search, and human-in-the-loop approval."
        ),
        version="0.1.0",
        docs_url="/docs",
        redoc_url="/redoc",
    )

    # ── CORS ────────────────────────────────────────────
    app.add_middleware(
        CORSMiddleware,
        allow_origins=os.environ.get("CORS_ORIGINS", "*").split(","),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── Auth ─────────────────────────────────────────────
    app.add_middleware(APIKeyMiddleware)

    # ── Health check ─────────────────────────────────────
    @app.get("/health")
    async def health_check():
        return {
            "status": "healthy",
            "service": "zuixiaoagent",
            "version": "0.1.0",
            "timestamp": datetime.utcnow().isoformat(),
            "python": sys.version,
            "platform": platform.platform(),
        }

    @app.get("/ping")
    async def ping():
        return {"pong": True}

    # ── Agent info ───────────────────────────────────────
    @app.get("/agent/info")
    async def agent_info(request: Request):
        """Return agent configuration and tool list."""
        from tools.registry import registry
        from agent.config import get_settings

        settings = get_settings()

        return {
            "model": settings.llm_model,
            "tools": [
                {
                    "name": tool.name,
                    "description": tool.description[:120],
                }
                for tool in registry.get_all()
            ],
            "memory": {
                "window_tokens": settings.memory_window_tokens,
                "compact_threshold": settings.memory_compact_threshold,
            },
            "shell": {
                "whitelist": sorted(settings.command_whitelist_set),
                "dangerous": sorted(settings.dangerous_commands_set),
            },
            "auth_enabled": settings.auth_enabled,
            "langsmith_enabled": settings.langsmith_tracing,
        }

    # ── Error handlers ───────────────────────────────────
    @app.exception_handler(Exception)
    async def global_exception_handler(request: Request, exc: Exception):
        logger.error(f"Unhandled exception: {exc}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={
                "error": "Internal server error",
                "detail": str(exc) if os.environ.get("DEBUG") else None,
            },
        )

    return app


# ── Module-level app instance ────────────────────────────────
# This is what langgraph.json http.app references.

app = create_app()
