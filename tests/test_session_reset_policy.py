"""Session resets must not be triggered by application errors or one timeout
(P0-09). A JSON-RPC error is not a transport failure, and resetting a shared
session aborts every other in-flight call on it."""

from __future__ import annotations

import asyncio

import httpx
from mcp import McpError
from mcp.types import CallToolResult, ErrorData, TextContent

from mcp_bridge.mcp_clients.AbstractClient import GenericMcpClient


class _Session:
    def __init__(self, behavior) -> None:  # type: ignore[no-untyped-def]
        self._behavior = behavior
        self.attempts = 0

    async def call_tool(self, name: str, arguments: dict[str, object]) -> CallToolResult:
        self.attempts += 1
        return await self._behavior(name, self.attempts)


class _Client(GenericMcpClient):
    def __init__(self, session: object) -> None:
        super().__init__("dummy")
        self._session_object = session
        self.session = session

    async def _maintain_session(self) -> None:
        return None

    async def _wait_for_session(
        self,
        timeout: int | None = None,
        http_error: bool = True,
        log_interval: float | None = None,
        poll_interval: float | None = None,
    ) -> None:
        if self.session is None:
            self.session = self._session_object


def _record_resets(monkeypatch, client: _Client) -> list[str]:  # type: ignore[no-untyped-def]
    resets: list[str] = []

    async def fake_reset(reason: str) -> None:
        resets.append(reason)

    monkeypatch.setattr(client, "_reset_session", fake_reset)
    return resets


def _ok(text: str = "ok") -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=text)], isError=False)


def test_jsonrpc_application_error_does_not_reset(monkeypatch) -> None:
    async def behavior(name: str, attempt: int) -> CallToolResult:
        raise McpError(ErrorData(code=-32602, message="Input validation error"))

    client = _Client(_Session(behavior))
    resets = _record_resets(monkeypatch, client)

    result = asyncio.run(client.call_tool("search", {}, timeout=1))

    assert result.isError is True
    assert resets == []


def test_transport_error_resets_the_session(monkeypatch) -> None:
    async def behavior(name: str, attempt: int) -> CallToolResult:
        raise httpx.ConnectError("connection refused")

    client = _Client(_Session(behavior))
    resets = _record_resets(monkeypatch, client)

    result = asyncio.run(client.call_tool("search", {}, timeout=1))

    assert result.isError is True
    assert len(resets) == 1
    assert "transport" in resets[0]


def test_one_timeout_does_not_reset_the_session(monkeypatch) -> None:
    async def behavior(name: str, attempt: int) -> CallToolResult:
        raise asyncio.TimeoutError()

    client = _Client(_Session(behavior))
    resets = _record_resets(monkeypatch, client)

    result = asyncio.run(client.call_tool("search", {}, timeout=1))

    assert result.isError is True
    assert resets == []


def test_three_consecutive_timeouts_trip_the_breaker(monkeypatch) -> None:
    async def behavior(name: str, attempt: int) -> CallToolResult:
        raise asyncio.TimeoutError()

    client = _Client(_Session(behavior))
    resets = _record_resets(monkeypatch, client)

    for _ in range(3):
        asyncio.run(client.call_tool("search", {}, timeout=1))

    assert len(resets) == 1
    assert "consecutive timeouts" in resets[0]


def test_success_clears_the_breaker_window(monkeypatch) -> None:
    async def behavior(name: str, attempt: int) -> CallToolResult:
        # timeout, timeout, success, timeout, timeout -> never three in a row.
        if attempt in {1, 2, 4, 5}:
            raise asyncio.TimeoutError()
        return _ok()

    client = _Client(_Session(behavior))
    resets = _record_resets(monkeypatch, client)

    for _ in range(5):
        asyncio.run(client.call_tool("search", {}, timeout=1))

    assert resets == []


def test_one_timeout_does_not_abort_another_in_flight_call(monkeypatch) -> None:
    async def behavior(name: str, attempt: int) -> CallToolResult:
        if name == "slow":
            raise asyncio.TimeoutError()
        await asyncio.sleep(0.01)
        return _ok("fast result")

    client = _Client(_Session(behavior))
    client.set_max_concurrent_calls(2)
    resets = _record_resets(monkeypatch, client)

    async def run():  # type: ignore[no-untyped-def]
        return await asyncio.gather(
            client.call_tool("slow", {}, timeout=1),
            client.call_tool("fast", {}, timeout=1),
        )

    slow_result, fast_result = asyncio.run(run())

    assert slow_result.isError is True
    assert fast_result.isError is False
    assert fast_result.content[0].text == "fast result"
    assert resets == []
