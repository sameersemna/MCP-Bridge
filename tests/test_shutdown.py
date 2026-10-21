"""Graceful shutdown must stop every MCP client, bounded by a timeout (P0-03).

Stdio servers own a child process, so exiting without calling ``stop()``
orphans it. Shutdown must also be concurrent: one hung client must not delay
the others beyond the shared budget.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager

from mcp_bridge import lifespan as lifespan_module
from mcp_bridge import telemetry
from mcp_bridge.mcp_clients.McpClientManager import MCPClientManager


class _FakeClient:
    def __init__(self, name: str, *, delay: float = 0.0, raises: bool = False) -> None:
        self.name = name
        self.delay = delay
        self.raises = raises
        self.stopped = False

    async def stop(self) -> None:
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.raises:
            raise RuntimeError(f"{self.name} failed to stop")
        self.stopped = True


def _manager_with(clients: dict[str, _FakeClient]) -> MCPClientManager:
    manager = MCPClientManager()
    # Instance attribute shadows the class-level registry, so tests never touch
    # the process-wide ClientManager singleton.
    manager.clients = dict(clients)
    return manager


def test_shutdown_stops_every_client_and_clears_the_registry() -> None:
    first = _FakeClient("first")
    second = _FakeClient("second")
    manager = _manager_with({"first": first, "second": second})

    asyncio.run(manager.shutdown(timeout=1.0))

    assert first.stopped and second.stopped
    assert manager.clients == {}


def test_shutdown_is_bounded_by_the_timeout_not_the_slowest_client() -> None:
    fast = _FakeClient("fast")
    slow = _FakeClient("slow", delay=10.0)
    manager = _manager_with({"fast": fast, "slow": slow})

    started = time.monotonic()
    asyncio.run(manager.shutdown(timeout=0.05))
    elapsed = time.monotonic() - started

    assert fast.stopped, "a slow client prevented the fast one from stopping"
    assert elapsed < 1.0, f"shutdown took {elapsed:.2f}s despite a 0.05s budget"
    assert manager.clients == {}


def test_a_failing_client_does_not_stop_the_others() -> None:
    broken = _FakeClient("broken", raises=True)
    healthy = _FakeClient("healthy")
    manager = _manager_with({"broken": broken, "healthy": healthy})

    asyncio.run(manager.shutdown(timeout=1.0))

    assert healthy.stopped
    assert manager.clients == {}


def test_lifespan_shuts_down_clients_then_pool_then_tracing(monkeypatch) -> None:
    calls: list[str] = []

    async def fake_initialize() -> None:
        calls.append("init")

    async def fake_shutdown() -> None:
        calls.append("clients")

    async def fake_close_pool() -> None:
        calls.append("http")

    def fake_shutdown_tracing() -> None:
        calls.append("tracing")

    monkeypatch.setattr(lifespan_module.ClientManager, "initialize", fake_initialize)
    monkeypatch.setattr(lifespan_module.ClientManager, "shutdown", fake_shutdown, raising=False)
    monkeypatch.setattr(lifespan_module, "close_shared_transport", fake_close_pool, raising=False)
    monkeypatch.setattr(lifespan_module, "shutdown_tracing", fake_shutdown_tracing, raising=False)

    @asynccontextmanager
    async def no_streamable_http_lifespan():
        yield

    monkeypatch.setattr(
        lifespan_module,
        "streamable_http_lifespan",
        no_streamable_http_lifespan,
        raising=False,
    )

    async def run() -> None:
        async with lifespan_module.lifespan(app=None):
            pass

    asyncio.run(run())
    assert calls == ["init", "clients", "http", "tracing"]


class _FakeProvider:
    def __init__(self) -> None:
        self.shutdown_calls = 0

    def shutdown(self) -> None:
        self.shutdown_calls += 1


def test_shutdown_tracing_is_idempotent(monkeypatch) -> None:
    provider = _FakeProvider()
    monkeypatch.setattr(telemetry._TracingState, "provider", provider, raising=False)

    telemetry.shutdown_tracing()
    telemetry.shutdown_tracing()

    assert provider.shutdown_calls == 1
