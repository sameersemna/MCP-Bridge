"""Streamable HTTP endpoint for the aggregated MCP server.

The deprecated HTTP+SSE transport remains at ``/mcp-server/sse``; current
clients (VS Code Copilot, Claude, Cursor) prefer Streamable HTTP, mounted at
``/mcp-server/mcp``.

The session manager is an ASGI application: it writes its own response, so it
must be *mounted* rather than used as a FastAPI route (a route would make
FastAPI send a second response). The wrapper below applies the same API-key
check as ``secure_router`` when authentication is enabled.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import HTTPException
from fastapi.responses import JSONResponse
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

from mcp_bridge.auth import simple_key_checker
from mcp_bridge.config import config

from .server import server

# One manager for the process: it owns the session table and must be started
# once (see ``streamable_http_lifespan``).
session_manager = StreamableHTTPSessionManager(app=server)


@asynccontextmanager
async def streamable_http_lifespan() -> AsyncIterator[None]:
    async with session_manager.run():
        yield


class StreamableHTTPApp:
    """ASGI wrapper: auth check, then the MCP Streamable HTTP transport."""

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") == "http" and config.security.auth.enabled:
            headers = {
                key.lower(): value for key, value in (scope.get("headers") or [])
            }
            raw = headers.get(b"authorization", b"").decode("utf-8", "replace")
            token = raw[7:].strip() if raw.lower().startswith("bearer ") else ""
            try:
                await simple_key_checker(token)
            except HTTPException as exc:
                response = JSONResponse(
                    status_code=exc.status_code,
                    content={"detail": exc.detail},
                )
                await response(scope, receive, send)
                return
        await session_manager.handle_request(scope, receive, send)


streamable_http_app = StreamableHTTPApp()
