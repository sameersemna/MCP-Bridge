"""Startup must start MCP servers concurrently (P1-05)."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

from mcp_bridge.config import config
from mcp_bridge.mcp_clients.McpClientManager import MCPClientManager


def _patch_servers(monkeypatch, names: list[str]) -> None:
    monkeypatch.setattr(
        config,
        "mcp_servers",
        {name: {"command": "true"} for name in names},
        raising=False,
    )
    monkeypatch.setattr(config, "disabled_mcp_servers", set(), raising=False)


def test_initialize_starts_servers_concurrently(monkeypatch) -> None:
    names = ["server-0", "server-1", "server-2"]
    _patch_servers(monkeypatch, names)
    manager = MCPClientManager()

    async def fake_construct(name: str, server_config: object):
        await asyncio.sleep(0.1)
        return SimpleNamespace(name=name)

    monkeypatch.setattr(manager, "construct_client", fake_construct)

    start = time.perf_counter()
    asyncio.run(manager.initialize())
    elapsed = time.perf_counter() - start

    # Serial startup would take ~0.3s; concurrent startup is ~0.1s.
    assert elapsed < 0.25, f"startup took {elapsed:.2f}s for three 0.1s servers"
    assert list(manager.clients) == names


def test_one_failed_server_does_not_block_the_others(monkeypatch) -> None:
    names = ["server-0", "server-1", "server-2"]
    _patch_servers(monkeypatch, names)
    manager = MCPClientManager()

    async def fake_construct(name: str, server_config: object):
        if name == "server-1":
            raise RuntimeError("boom")
        return SimpleNamespace(name=name)

    monkeypatch.setattr(manager, "construct_client", fake_construct)

    asyncio.run(manager.initialize())

    assert set(manager.clients) == {"server-0", "server-2"}
