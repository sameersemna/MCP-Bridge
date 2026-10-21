"""The aggregated MCP server must be reachable over Streamable HTTP (P1-07)."""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from sse_starlette.sse import AppStatus

from mcp_bridge.mcp_server.server import server


@pytest.fixture(autouse=True)
def _fresh_sse_app_status() -> None:
    # sse-starlette caches an anyio Event bound to the first loop; TestClient
    # uses a fresh loop per run.
    AppStatus.should_exit_event = None
    AppStatus.should_exit = False
    AppStatus.original_handler = None


def test_initialize_over_streamable_http_returns_a_session() -> None:
    # A local manager, not the process-global one: a session manager can only
    # be run once, and sharing it across tests couples their lifecycles.
    manager = StreamableHTTPSessionManager(app=server)

    @asynccontextmanager
    async def lifespan(app):  # type: ignore[no-untyped-def]
        async with manager.run():
            yield

    app = FastAPI(lifespan=lifespan)
    app.mount("/mcp", manager.handle_request)

    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "test-client", "version": "1.0"},
        },
    }
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }

    with TestClient(app) as client:
        response = client.post("/mcp", json=payload, headers=headers)

    assert response.status_code == 200, response.text
    assert "protocolVersion" in response.text
    assert response.headers.get("mcp-session-id")
