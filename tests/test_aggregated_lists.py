"""Aggregated prompt/resource lists run concurrently and isolate failures (P3-02)."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import mcp_bridge.mcp_server.server as server_module
from mcp_bridge.mcp_clients.McpClientManager import ClientManager


class _Client:
    def __init__(self, name: str, *, delay: float = 0.0, raises: bool = False) -> None:
        self.name = name
        self.delay = delay
        self.raises = raises

    async def list_prompts(self):  # type: ignore[no-untyped-def]
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.raises:
            raise RuntimeError("boom")
        return SimpleNamespace(prompts=[SimpleNamespace(name=self.name)])

    async def list_resources(self):  # type: ignore[no-untyped-def]
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.raises:
            raise RuntimeError("boom")
        return SimpleNamespace(resources=[SimpleNamespace(uri=f"file:///{self.name}")])


def test_list_prompts_is_concurrent_and_isolates_failures(monkeypatch) -> None:
    clients = {
        "a": _Client("a", delay=0.1),
        "b": _Client("b", delay=0.1),
        "bad": _Client("bad", raises=True),
    }
    monkeypatch.setattr(ClientManager, "clients", clients, raising=False)

    start = time.perf_counter()
    prompts = asyncio.run(server_module.list_prompts())
    elapsed = time.perf_counter() - start

    assert {prompt.name for prompt in prompts} == {"a", "b"}
    assert elapsed < 0.18, f"prompt listing was not concurrent ({elapsed:.2f}s)"


def test_list_resources_isolates_failures(monkeypatch) -> None:
    clients = {
        "a": _Client("a"),
        "bad": _Client("bad", raises=True),
    }
    monkeypatch.setattr(ClientManager, "clients", clients, raising=False)

    resources = asyncio.run(server_module.list_resources())

    assert [str(resource.uri) for resource in resources] == ["file:///a"]
