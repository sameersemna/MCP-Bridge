"""Streamable HTTP session correctness (P0-08).

The hand-rolled client discarded the session id, never sent the negotiated
protocol version or configured headers, opened a client per message, never
recovered from a 404, and never deleted the session. These tests use an
in-process httpx transport, so nothing touches the network.
"""

from __future__ import annotations

import asyncio
import json

import httpx

import mcp_bridge
import mcp_bridge.mcp_clients.SseClient as sse_module
from mcp_bridge.config.final import SSEMCPServer
from mcp_bridge.mcp_clients.McpClientManager import _redact_server_config_for_logging

_PROTOCOL_VERSION = "2025-06-18"


def _body(request: httpx.Request) -> dict[str, object]:
    if not request.content:
        return {}
    return json.loads(request.content.decode("utf-8"))


class _Recorder(httpx.AsyncBaseTransport):
    def __init__(self, handler):  # type: ignore[no-untyped-def]
        self._handler = handler
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._handler(request)


def _initialize_payload(request_id: object) -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {
            "protocolVersion": _PROTOCOL_VERSION,
            "capabilities": {},
            "serverInfo": {"name": "test-server", "version": "1.0"},
        },
    }


def _happy_handler(request: httpx.Request) -> httpx.Response:
    body = _body(request)
    method = body.get("method") or request.method
    headers = {"content-type": "application/json", "mcp-session-id": "sess-123"}
    if method == "initialize":
        return httpx.Response(
            200, json=_initialize_payload(body.get("id")), headers=headers, request=request
        )
    if method == "notifications/initialized":
        return httpx.Response(202, headers=headers, request=request)
    if method == "tools/list":
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": body.get("id"), "result": {"tools": []}},
            headers=headers,
            request=request,
        )
    if request.method == "DELETE":
        return httpx.Response(200, headers=headers, request=request)
    return httpx.Response(
        200,
        json={"jsonrpc": "2.0", "id": body.get("id"), "result": {}},
        headers=headers,
        request=request,
    )


def test_session_id_and_protocol_version_are_echoed() -> None:
    recorder = _Recorder(_happy_handler)
    session = sse_module.HttpMcpSession(url="https://example.com/mcp", transport=recorder)

    async def run() -> None:
        await session.initialize()
        await session.list_tools()

    asyncio.run(run())

    initialize = next(r for r in recorder.requests if _body(r).get("method") == "initialize")
    assert "mcp-session-id" not in initialize.headers
    assert "mcp-protocol-version" not in initialize.headers

    tools = next(r for r in recorder.requests if _body(r).get("method") == "tools/list")
    assert tools.headers.get("mcp-session-id") == "sess-123"
    assert tools.headers.get("mcp-protocol-version") == _PROTOCOL_VERSION


def test_configured_headers_are_sent() -> None:
    recorder = _Recorder(_happy_handler)
    session = sse_module.HttpMcpSession(
        url="https://example.com/mcp",
        headers={"Authorization": "Bearer secret", "X-Tenant": "acme"},
        transport=recorder,
    )

    asyncio.run(session.initialize())

    initialize = recorder.requests[0]
    assert initialize.headers.get("authorization") == "Bearer secret"
    assert initialize.headers.get("x-tenant") == "acme"


def test_one_http_client_is_reused_for_the_session() -> None:
    recorder = _Recorder(_happy_handler)
    session = sse_module.HttpMcpSession(url="https://example.com/mcp", transport=recorder)

    async def run() -> None:
        await session.initialize()
        first_client = session._client
        await session.list_tools()
        second_client = session._client
        assert first_client is second_client
        assert first_client is not None
        assert first_client.is_closed is False

    asyncio.run(run())


def test_404_reinitializes_once_and_retries() -> None:
    state = {"initialize": 0, "tools": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        body = _body(request)
        method = body.get("method") or request.method
        if method == "initialize":
            state["initialize"] += 1
            return httpx.Response(
                200,
                json=_initialize_payload(body.get("id")),
                headers={
                    "content-type": "application/json",
                    "mcp-session-id": f"sess-{state['initialize']}",
                },
                request=request,
            )
        if method == "notifications/initialized":
            return httpx.Response(202, request=request)
        if method == "tools/list":
            state["tools"] += 1
            if state["tools"] == 1:
                return httpx.Response(404, text="session expired", request=request)
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": body.get("id"), "result": {"tools": []}},
                headers={"content-type": "application/json"},
                request=request,
            )
        return httpx.Response(200, json={}, request=request)

    recorder = _Recorder(handler)
    session = sse_module.HttpMcpSession(url="https://example.com/mcp", transport=recorder)

    async def run():  # type: ignore[no-untyped-def]
        await session.initialize()
        return await session.list_tools()

    result = asyncio.run(run())

    assert result.tools == []
    assert state["initialize"] == 2
    assert state["tools"] == 2
    retried = [r for r in recorder.requests if _body(r).get("method") == "tools/list"][-1]
    assert retried.headers.get("mcp-session-id") == "sess-2"


def test_close_deletes_the_session_and_closes_the_client() -> None:
    recorder = _Recorder(_happy_handler)
    session = sse_module.HttpMcpSession(url="https://example.com/mcp", transport=recorder)

    async def run() -> None:
        await session.initialize()
        assert session._client is not None
        await session.aclose()

    asyncio.run(run())

    deletes = [r for r in recorder.requests if r.method == "DELETE"]
    assert len(deletes) == 1
    assert deletes[0].headers.get("mcp-session-id") == "sess-123"
    assert session._client is None


def test_client_info_uses_the_package_version() -> None:
    recorder = _Recorder(_happy_handler)
    session = sse_module.HttpMcpSession(url="https://example.com/mcp", transport=recorder)

    asyncio.run(session.initialize())

    initialize = next(r for r in recorder.requests if _body(r).get("method") == "initialize")
    assert _body(initialize)["params"]["clientInfo"]["version"] == mcp_bridge.__version__


def test_configured_headers_and_auth_are_redacted_in_logs() -> None:
    server = SSEMCPServer(
        url="https://example.com/mcp",
        headers={"Authorization": "Bearer super-secret", "X-Custom": "another-secret"},
        auth={"type": "bearer", "token": "token-secret"},
    )

    redacted = _redact_server_config_for_logging(server)
    assert redacted["headers"] == {
        "Authorization": "[REDACTED]",
        "X-Custom": "[REDACTED]",
    }
    assert redacted["auth"] == {"type": "bearer", "token": "[REDACTED]"}
    assert "super-secret" not in json.dumps(redacted)
    assert "another-secret" not in json.dumps(redacted)
    assert "token-secret" not in json.dumps(redacted)


def test_bearer_auth_is_mapped_to_an_authorization_header() -> None:
    server = SSEMCPServer(
        url="https://example.com/mcp",
        auth={"type": "bearer", "token": "abc123"},
    )
    headers = sse_module._server_headers(server)
    assert headers["Authorization"] == "Bearer abc123"


def test_aggregated_server_reports_the_package_version() -> None:
    import mcp_bridge.mcp_server.server as server_module

    assert server_module.options.server_version == mcp_bridge.__version__
