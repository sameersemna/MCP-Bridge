"""The upstream connection pool must outlive individual requests (P0-02).

httpx 0.28's ``AsyncClient.aclose()`` unconditionally closes the transport it
was given. Because every per-request client was handed the process-wide
transport, each request tore down the shared keep-alive pool. These tests pin
the pool's lifetime to the app lifespan instead.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx

from mcp_bridge import lifespan as lifespan_module
from mcp_bridge.openai_clients import genericHttpxClient as generic_httpx


class _RecordingTransport(httpx.AsyncBaseTransport):
    """A transport that answers locally and counts closes."""

    def __init__(self) -> None:
        self.close_calls = 0
        self.request_count = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.request_count += 1
        return httpx.Response(200, json={"ok": True}, request=request)

    async def aclose(self) -> None:
        self.close_calls += 1


def test_shared_transport_survives_sequential_request_clients(monkeypatch) -> None:
    transport = _RecordingTransport()
    monkeypatch.setattr(generic_httpx, "_shared_transport", transport)

    async def run() -> tuple[int, int]:
        async with generic_httpx.get_client() as first:
            first_response = await first.get("/models")
        async with generic_httpx.get_client() as second:
            second_response = await second.get("/models")
        return first_response.status_code, second_response.status_code

    assert asyncio.run(run()) == (200, 200)
    assert transport.request_count == 2
    assert transport.close_calls == 0, "a per-request client closed the shared pool"


def test_close_shared_transport_closes_once_and_resets(monkeypatch) -> None:
    transport = _RecordingTransport()
    monkeypatch.setattr(generic_httpx, "_shared_transport", transport)

    async def run() -> None:
        await generic_httpx.close_shared_transport()
        # A second shutdown (or a late request) must not explode.
        await generic_httpx.close_shared_transport()

    asyncio.run(run())
    assert transport.close_calls == 1
    assert generic_httpx._shared_transport is None


def test_concurrent_clients_keep_their_own_headers(monkeypatch) -> None:
    transport = _RecordingTransport()
    monkeypatch.setattr(generic_httpx, "_shared_transport", transport)

    async def one(user: str) -> tuple[str, int]:
        request = SimpleNamespace(headers={"x-openwebui-user-name": user})
        async with generic_httpx.get_client(request) as client:
            response = await client.get("/models")
            return client.headers["x-openwebui-user-name"], response.status_code

    async def run() -> list[tuple[str, int]]:
        return await asyncio.gather(one("alice"), one("bob"))

    results = asyncio.run(run())

    assert results == [("alice", 200), ("bob", 200)]
    assert transport.close_calls == 0


def test_lifespan_shutdown_closes_the_shared_transport(monkeypatch) -> None:
    transport = _RecordingTransport()
    monkeypatch.setattr(generic_httpx, "_shared_transport", transport)

    async def fake_initialize() -> None:
        return None

    monkeypatch.setattr(lifespan_module.ClientManager, "initialize", fake_initialize)

    async def run() -> None:
        async with lifespan_module.lifespan(app=None):
            pass

    asyncio.run(run())
    assert transport.close_calls == 1, "shutdown did not close the upstream pool"
