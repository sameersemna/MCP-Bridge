"""A client disconnect must stop tool work and notify downstream (P1-08)."""

from __future__ import annotations

import asyncio

import anyio
import pytest

from mcp_bridge.cancellation import ClientDisconnected, run_until_disconnected
from mcp_bridge.mcp_clients.McpClientManager import MCPClientManager
from mcp_bridge.mcp_clients.SseClient import SseMcpSession


class _Request:
    def __init__(self, disconnect_after_checks: int) -> None:
        self._remaining = disconnect_after_checks

    async def is_disconnected(self) -> bool:
        await asyncio.sleep(0.005)
        self._remaining -= 1
        return self._remaining <= 0


def test_run_until_disconnected_returns_the_result() -> None:
    async def work() -> int:
        return 42

    result = asyncio.run(
        run_until_disconnected(work(), _Request(1000), poll_interval=0.005)
    )

    assert result == 42


def test_run_until_disconnected_cancels_the_work() -> None:
    cancelled: list[int] = []

    async def work() -> None:
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.append(1)
            raise

    with pytest.raises(ClientDisconnected):
        asyncio.run(
            run_until_disconnected(work(), _Request(2), poll_interval=0.005)
        )

    assert cancelled == [1]


def test_manager_notifies_every_client() -> None:
    calls: list[str] = []

    class _Client:
        async def notify_cancelled(self, reason: str = "") -> None:
            calls.append(reason)

    manager = MCPClientManager()
    manager.clients = {"a": _Client(), "b": _Client()}

    asyncio.run(manager.notify_cancelled())

    assert calls == ["client disconnected", "client disconnected"]


def test_sse_session_emits_a_cancelled_notification() -> None:
    async def run():  # type: ignore[no-untyped-def]
        read_writer, read_stream = anyio.create_memory_object_stream(0)
        write_stream, write_reader = anyio.create_memory_object_stream(0)
        async with SseMcpSession(read_stream, write_stream) as session:
            # The write stream has zero buffer: start the receiver before
            # awaiting the send, otherwise both sides block forever.
            receive_task = asyncio.create_task(write_reader.receive())
            send_task = asyncio.create_task(
                session.notify_cancelled("client went away")
            )
            message = await asyncio.wait_for(receive_task, 1)
            await asyncio.wait_for(send_task, 1)
        return message.root

    root = asyncio.run(run())

    assert root.method == "notifications/cancelled"
    assert root.params["reason"] == "client went away"