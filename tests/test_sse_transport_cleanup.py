"""Disconnected SSE sessions must be removed and their streams closed (P0-06).

The vendored transport stores a writer per session and, like mcp 1.3.0, never
removed it. A long-lived bridge therefore leaked one memory stream per SSE
connection, and a POST to a dead session could still find a writer.
"""

from __future__ import annotations

import asyncio

import anyio
import pytest
from sse_starlette.sse import AppStatus
from starlette.requests import Request

from mcp_bridge.mcp_server.sse_transport import SseServerTransport


@pytest.fixture(autouse=True)
def _fresh_sse_app_status() -> None:
    """sse-starlette caches an anyio Event that binds to the first loop.

    Each test drives its own ``asyncio.run`` loop, so reset the process-global
    event to let it be recreated on the current loop.
    """
    AppStatus.should_exit_event = None
    AppStatus.should_exit = False
    AppStatus.original_handler = None


def _request(sent: list[dict[str, object]]) -> Request:
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/mcp-server/sse",
        "raw_path": b"/mcp-server/sse",
        "query_string": b"",
        "root_path": "",
        "headers": [],
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
    }

    async def receive() -> dict[str, object]:
        # Starlette reports a client disconnect this way; EventSourceResponse
        # exits on it, which is exactly the path that must clean the session.
        return {"type": "http.disconnect"}

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    return Request(scope, receive, send)


def test_disconnected_session_is_removed_and_its_streams_closed() -> None:
    async def run() -> None:
        transport = SseServerTransport("/messages")
        request = _request([])

        async with transport.connect_sse(request) as (read_stream, write_stream):
            # Control: the session is registered while the connection is open,
            # so an empty map after exit means cleanup, not a no-op.
            assert len(transport._read_stream_writers) == 1
            _, writer = next(iter(transport._read_stream_writers.items()))

        assert transport._read_stream_writers == {}

        # Both ends of both streams are closed, so the server handler unblocks
        # instead of waiting on a stream nobody owns. ``*_nowait`` is used so a
        # regression fails as WouldBlock instead of hanging the suite.
        with pytest.raises(anyio.ClosedResourceError):
            writer.send_nowait(None)
        with pytest.raises(anyio.ClosedResourceError):
            read_stream.receive_nowait()
        with pytest.raises(anyio.ClosedResourceError):
            write_stream.send_nowait(None)

    asyncio.run(run())


def test_many_open_and_close_cycles_leave_an_empty_map() -> None:
    async def run() -> None:
        transport = SseServerTransport("/messages")
        for _ in range(5):
            async with transport.connect_sse(_request([])):
                assert len(transport._read_stream_writers) == 1
            assert transport._read_stream_writers == {}

    asyncio.run(run())
