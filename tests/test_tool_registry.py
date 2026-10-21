"""Tool resolution must read a warm registry, not re-probe every server (P1-01)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import anyio
import mcp.types as types

from mcp_bridge.mcp_clients.AbstractClient import GenericMcpClient
from mcp_bridge.mcp_clients.McpClientManager import MCPClientManager
from mcp_bridge.mcp_clients.SseClient import SseMcpSession
from mcp_bridge.mcp_clients.ToolRegistry import ToolRegistry


class _Tool:
    def __init__(self, name: str) -> None:
        self.name = name
        self.description = None
        self.inputSchema = {"type": "object"}


class _Session:
    def __init__(self, tools: list[_Tool]) -> None:
        self.tools = tools
        self.calls = 0

    async def list_tools(self):  # type: ignore[no-untyped-def]
        self.calls += 1
        return SimpleNamespace(tools=self.tools)


class _Client:
    def __init__(self, name: str, tools: list[_Tool]) -> None:
        self.name = name
        self.session = _Session(tools)
        self._tool_annotations: dict[str, object] = {}


def _manager(clients: dict[str, _Client]) -> MCPClientManager:
    manager = MCPClientManager()
    manager.clients = dict(clients)
    manager.registry = ToolRegistry(ttl_seconds=300)
    return manager


def test_resolution_makes_no_list_tools_rpcs_once_warm() -> None:
    first = _Client("first", [_Tool("alpha")])
    second = _Client("second", [_Tool("beta")])
    manager = _manager({"first": first, "second": second})

    async def run():  # type: ignore[no-untyped-def]
        await manager.refresh_tool_registry(force=True)
        first.session.calls = 0
        second.session.calls = 0
        one = await manager.resolve_tool("alpha")
        two = await manager.resolve_tool("alpha")
        return one, two

    one, two = asyncio.run(run())

    assert one is not None and one[0] is first and one[1] == "alpha"
    assert two == one
    assert first.session.calls == 0
    assert second.session.calls == 0


def test_collision_resolves_to_the_first_configured_server() -> None:
    first = _Client("first", [_Tool("search")])
    second = _Client("second", [_Tool("search")])
    manager = _manager({"first": first, "second": second})

    async def run():  # type: ignore[no-untyped-def]
        await manager.refresh_tool_registry(force=True)
        return await manager.resolve_tool("search")

    resolved = asyncio.run(run())

    assert resolved is not None
    assert resolved[0] is first
    assert resolved[1] == "search"


def test_ttl_zero_is_always_stale_and_positive_ttl_is_warm() -> None:
    stale = ToolRegistry(ttl_seconds=0)
    stale.replace([])
    assert stale.is_stale() is True

    warm = ToolRegistry(ttl_seconds=300)
    warm.replace([])
    assert warm.is_stale() is False


def test_tools_list_changed_notification_invalidates_the_registry() -> None:
    async def run() -> list[int]:
        read_writer, read_stream = anyio.create_memory_object_stream(0)
        write_stream, write_reader = anyio.create_memory_object_stream(0)
        calls: list[int] = []
        async with SseMcpSession(
            read_stream, write_stream, on_tools_changed=lambda: calls.append(1)
        ):
            await read_writer.send(
                types.JSONRPCMessage(
                    types.JSONRPCNotification(
                        jsonrpc="2.0",
                        method="notifications/tools/list_changed",
                        params={},
                    )
                )
            )
            for _ in range(100):
                if calls:
                    break
                await asyncio.sleep(0.01)
        return calls

    assert asyncio.run(run()) == [1]


def test_session_reset_invalidates_the_registry() -> None:
    calls: list[int] = []

    class _ClientUnderTest(GenericMcpClient):
        async def _maintain_session(self) -> None:
            return None

    client = _ClientUnderTest("server")
    client.on_tools_changed = lambda: calls.append(1)

    asyncio.run(client._reset_session("test"))

    assert calls == [1]


def test_suggestions_use_the_registry_without_rpcs() -> None:
    client = _Client("first", [_Tool("google_search")])
    manager = _manager({"first": client})

    async def run():  # type: ignore[no-untyped-def]
        await manager.refresh_tool_registry(force=True)
        client.session.calls = 0
        return await manager.suggest_tools("google_serch")

    suggestions = asyncio.run(run())

    assert "google_search" in suggestions
    assert client.session.calls == 0


def test_registry_ttl_can_be_configured_by_environment(monkeypatch) -> None:
    monkeypatch.setenv("MCP_BRIDGE_TOOL_REGISTRY_TTL_SECONDS", "1")
    assert ToolRegistry()._ttl_seconds == 1.0
