"""API Key authentication middleware for LangGraph Server.

Protects the HTTP API with X-API-Key header validation.
When AGENT_API_KEY is not set, authentication is disabled (dev mode).
"""

from __future__ import annotations

import logging
import os

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)


class APIKeyMiddleware(BaseHTTPMiddleware):
    """Middleware that validates X-API-Key header on all requests.

    Skips validation for:
      - Health check endpoints (/health, /ping)
      - OPTIONS requests (CORS preflight)
      - When AGENT_API_KEY is not configured (dev mode)
    """

    HEALTH_PATHS = {"/health", "/ping", "/ok", "/ready"}
    DOCS_PATHS = {"/docs", "/openapi.json", "/redoc"}

    async def dispatch(self, request: Request, call_next):
        # Skip auth for health checks
        if request.url.path in self.HEALTH_PATHS:
            return await call_next(request)

        # Skip auth for CORS preflight
        if request.method == "OPTIONS":
            return await call_next(request)

        # Skip auth for API docs (dev convenience)
        if request.url.path in self.DOCS_PATHS:
            return await call_next(request)

        # Check API key
        api_key = os.environ.get("AGENT_API_KEY", "")
        if not api_key:
            # No key configured → auth disabled (dev mode)
            return await call_next(request)

        request_key = request.headers.get("X-API-Key", "")
        if not request_key:
            return JSONResponse(
                status_code=401,
                content={
                    "error": "Missing X-API-Key header",
                    "hint": "Include your API key in the X-API-Key header",
                },
            )

        if request_key != api_key:
            logger.warning(
                f"Invalid API key from {request.client.host if request.client else 'unknown'}"
            )
            return JSONResponse(
                status_code=403,
                content={"error": "Invalid API key"},
            )

        return await call_next(request)
