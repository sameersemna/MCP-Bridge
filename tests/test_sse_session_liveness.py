"""Regression tests for the SSE session teardown / liveness bugs.

These cover a real production failure chain observed in the bridge logs:

    DEBUGIMP finish reason: tool_calls; tool_calls=True
    WARNING  tool loop context budget exceeded (99224 > 96000 tokens); stopping ...
    ERROR    ping failed for google-search: TimeoutError
    ERROR    failed to maintain session for google-search:
             <class 'ExceptionGroup'> ('unhandled errors in a TaskGroup',
                                        [InvalidStateError('invalid state')])

Root causes fixed here:

1. ``asyncio.wait_for`` CANCELS the awaited future the instant it times out,
   while the ``finally`` that removes it from ``_pending_responses`` only runs
   on the next event-loop tick. A reply arriving in that window called
   ``set_result()`` on an already-cancelled future, raising
   ``asyncio.InvalidStateError`` *inside the message-loop task*.
2. ``__aexit__`` awaited that dead task while the original ``TimeoutError`` was
   already propagating, so the secondary ``InvalidStateError`` REPLACED it.
   The real cause was never logged, and the misleading ``ExceptionGroup`` was
   not classified as a transport error (so it took the non-retry ``ERROR``
   path instead of the retry ``WARNING`` path).
3. The keep-alive ping reused the 120s ``requestTimeout`` (a *data* timeout) as
   its liveness budget and fired even while tool calls were in flight, so a
   ping queued behind a slow search declared a healthy session dead.
"""

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp_bridge.mcp_clients import SseClient as sse_client_module
from mcp_bridge.mcp_clients.AbstractClient import GenericMcpClient
from mcp_bridge.mcp_clients.SseClient import (
    DEFAULT_SSE_PING_TIMEOUT_SECONDS,
    SseMcpSession,
    get_sse_ping_timeout_seconds,
)


class _FakeStream:
    """Minimal stand-in for an anyio memory object stream."""

    def __init__(self) -> None:
        self.sent: list[Any] = []
        self._incoming: asyncio.Queue[Any] = asyncio.Queue()

    async def send(self, item: Any) -> None:
        self.sent.append(item)

    async def receive(self) -> Any:
        return await self._incoming.get()

    def push(self, item: Any) -> None:
        self._incoming.put_nowait(item)


def _jsonrpc_response(message_id: int, result: Any) -> Any:
    """Build a JSONRPCMessage wrapping a JSONRPCResponse, mirroring the SDK shape."""
    types = sse_client_module.types
    return types.JSONRPCMessage(
        types.JSONRPCResponse(jsonrpc="2.0", id=message_id, result=result)
    )


def _session() -> tuple[SseMcpSession, _FakeStream, _FakeStream]:
    read_stream = _FakeStream()
    write_stream = _FakeStream()
    session = SseMcpSession(read_stream, write_stream)
    return session, read_stream, write_stream


# ---------------------------------------------------------------------------
# 1. The cancelled-future race (InvalidStateError)
# ---------------------------------------------------------------------------


def test_late_reply_to_cancelled_future_does_not_raise_invalid_state_error():
    """A reply that lands AFTER the wait timed out must be ignored silently.

    This is the precise race that killed the message loop: ``wait_for`` cancels
    the future, then the late reply still finds the id in ``_pending_responses``
    and calls ``set_result`` on the cancelled future.
    """
    session, _, _ = _session()

    async def run() -> None:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        session._pending_responses[1] = future

        # Simulate `wait_for` having cancelled the future on timeout, WITHOUT
        # the `finally` having popped the map entry yet.
        future.cancel()
        assert future.cancelled()

        # Previously this raised asyncio.InvalidStateError('invalid state'),
        # killing the message-loop task.
        session._resolve_future(1, result=object())

        # The late reply is dropped and the entry is cleaned up.
        assert 1 not in session._pending_responses

    asyncio.run(run())


def test_late_error_reply_to_cancelled_future_is_ignored():
    """Same race, but for a JSONRPCError reply (set_exception path)."""
    session, _, _ = _session()

    async def run() -> None:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        session._pending_responses[7] = future
        future.cancel()

        session._resolve_future(7, exception=RuntimeError("boom"))

        assert 7 not in session._pending_responses

    asyncio.run(run())


def test_resolve_future_settles_a_pending_future_normally():
    """The happy path must still work: a pending future is resolved."""
    session, _, _ = _session()

    async def run() -> None:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        session._pending_responses[3] = future

        session._resolve_future(3, result="payload")

        assert await future == "payload"

    asyncio.run(run())


def test_unknown_request_id_is_ignored():
    session, _, _ = _session()

    async def run() -> None:
        # No entry registered -> must be a harmless no-op, not a KeyError.
        session._resolve_future(999, result="nope")

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 2. Teardown must not replace the in-flight exception
# ---------------------------------------------------------------------------


def test_aexit_does_not_replace_in_flight_exception_with_message_loop_error():
    """The message loop failing during teardown must not mask the real error.

    Reproduces the exact production symptom: a ping ``TimeoutError`` was in
    flight, and ``__aexit__``'s ``await self._message_task`` re-raised an
    ``InvalidStateError`` from the dead loop -- replacing the ``TimeoutError``
    entirely so the logs showed only the ``ExceptionGroup``.
    """
    read_stream = _FakeStream()
    write_stream = _FakeStream()
    session = SseMcpSession(read_stream, write_stream)

    async def run() -> None:
        # Make the message loop die with InvalidStateError, exactly as the race
        # did, by raising from `receive()`.
        async def failing_receive() -> Any:
            raise asyncio.InvalidStateError("invalid state")

        read_stream.receive = failing_receive  # type: ignore[method-assign]

        await session.__aenter__()
        # Let the loop task start and die with the error.
        await asyncio.sleep(0.05)
        assert session._message_task is not None
        assert session._message_task.done()

        # Now unwind with a TimeoutError in flight (what the ping raised).
        timeout_error = TimeoutError("ping timeout")
        try:
            raise timeout_error
        except TimeoutError:
            # Previously this call raised the InvalidStateError, replacing the
            # TimeoutError. It must now return normally so the original
            # exception keeps propagating.
            await session.__aexit__(TimeoutError, timeout_error, None)

    asyncio.run(run())


def test_aexit_fails_pending_futures_so_callers_do_not_hang():
    """In-flight requests must be failed on teardown, not left awaiting forever."""
    session, _, _ = _session()

    async def run() -> None:
        loop = asyncio.get_running_loop()
        pending: asyncio.Future[Any] = loop.create_future()
        session._pending_responses[42] = pending

        await session.__aenter__()
        await session.__aexit__(None, None, None)

        assert session._pending_responses == {}
        assert pending.cancelled()

    asyncio.run(run())


def test_aexit_clears_message_task_and_is_idempotent():
    session, _, _ = _session()

    async def run() -> None:
        await session.__aenter__()
        await session.__aexit__(None, None, None)
        assert session._message_task is None

        # A second teardown must be a harmless no-op.
        await session.__aexit__(None, None, None)

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 3. Ping timeout must be a short liveness leash, not the data timeout
# ---------------------------------------------------------------------------


def test_ping_uses_short_dedicated_timeout_not_read_timeout(monkeypatch):
    """A ping must not inherit the (potentially huge) data read timeout.

    With ``requestTimeout: 120000`` the ping previously waited the full 120s
    before declaring the session dead.
    """
    monkeypatch.delenv("MCP_BRIDGE_SSE_PING_TIMEOUT_SECONDS", raising=False)
    read_stream = _FakeStream()
    write_stream = _FakeStream()
    # Data timeout of 120s (google-search's requestTimeout).
    session = SseMcpSession(read_stream, write_stream, read_timeout_seconds=120.0)

    assert session._read_timeout_seconds == 120.0
    # But pings get the short liveness leash instead.
    assert session._ping_timeout_seconds == DEFAULT_SSE_PING_TIMEOUT_SECONDS
    assert session._ping_timeout_seconds < session._read_timeout_seconds


def test_ping_timeout_can_be_overridden_explicitly():
    read_stream = _FakeStream()
    write_stream = _FakeStream()
    session = SseMcpSession(
        read_stream, write_stream, read_timeout_seconds=120.0, ping_timeout_seconds=5.0
    )

    assert session._ping_timeout_seconds == 5.0


def test_ping_timeout_reads_environment_override(monkeypatch):
    monkeypatch.setenv("MCP_BRIDGE_SSE_PING_TIMEOUT_SECONDS", "45")
    assert get_sse_ping_timeout_seconds() == 45.0

    read_stream = _FakeStream()
    write_stream = _FakeStream()
    session = SseMcpSession(read_stream, write_stream)
    assert session._ping_timeout_seconds == 45.0


def test_ping_timeout_falls_back_on_invalid_environment(monkeypatch):
    monkeypatch.setenv("MCP_BRIDGE_SSE_PING_TIMEOUT_SECONDS", "not-a-number")
    assert get_sse_ping_timeout_seconds() == DEFAULT_SSE_PING_TIMEOUT_SECONDS

    monkeypatch.setenv("MCP_BRIDGE_SSE_PING_TIMEOUT_SECONDS", "-5")
    assert get_sse_ping_timeout_seconds() == DEFAULT_SSE_PING_TIMEOUT_SECONDS

    monkeypatch.delenv("MCP_BRIDGE_SSE_PING_TIMEOUT_SECONDS", raising=False)
    assert get_sse_ping_timeout_seconds() == DEFAULT_SSE_PING_TIMEOUT_SECONDS


def test_ping_timeout_can_be_disabled_with_none():
    """None is an explicit "no ping timeout" and must not be read as unset."""
    read_stream = _FakeStream()
    write_stream = _FakeStream()
    session = SseMcpSession(
        read_stream, write_stream, read_timeout_seconds=120.0, ping_timeout_seconds=None
    )

    assert session._ping_timeout_seconds is None


def test_send_ping_applies_its_own_timeout(monkeypatch):
    """A ping whose reply never arrives must time out on the PING budget.

    Proves the timeout is actually wired through `send_ping` -> `_send_request`
    rather than only stored on the object.
    """
    import mcp.types as types

    async def run() -> None:
        read_stream = _FakeStream()
        write_stream = _FakeStream()
        session = SseMcpSession(
            read_stream,
            write_stream,
            read_timeout_seconds=3600.0,  # data timeout would hang the test
            ping_timeout_seconds=0.05,
        )

        await session.__aenter__()
        try:
            # No reply is ever pushed -> only the ping timeout can end this.
            with pytest.raises((TimeoutError, asyncio.TimeoutError)):
                await session.send_ping()
        finally:
            await session.__aexit__(None, None, None)

        # The ping request was actually written before timing out.
        assert any(
            getattr(getattr(item, "root", None), "method", None) == "ping"
            for item in write_stream.sent
        )
        _ = types

    asyncio.run(run())


def test_late_ping_reply_after_timeout_does_not_break_loop():
    """End-to-end: ping times out, then its reply arrives late.

    The late reply must be discarded without raising, and the message loop must
    still be alive so the session is not spuriously torn down.
    """
    write_stream = _FakeStream()
    session, read_stream, _ = _session()
    session._ping_timeout_seconds = 0.05

    async def run() -> None:
        await session.__aenter__()

        with pytest.raises((TimeoutError, asyncio.TimeoutError)):
            await session.send_ping()

        # Deliver the ping's reply *after* the timeout, as a slow server would.
        read_stream.push(_jsonrpc_response(1, {}))

        # Give the message loop a chance to process the late reply.
        await asyncio.sleep(0.05)

        # Loop survived; no InvalidStateError killed it.
        assert session._message_task is not None
        assert not session._message_task.done()

        await session.__aexit__(None, None, None)

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 4. Ping suppression while tool calls are in flight
# ---------------------------------------------------------------------------


class _DummyClient(GenericMcpClient):
    async def _maintain_session(self) -> None:  # pragma: no cover - not used
        return None


def test_pings_suppressed_while_tool_call_in_flight():
    """An in-flight tool call proves liveness, so pings must be skipped.

    This is what stopped a healthy `google-search` session from being torn down
    by a ping that merely queued behind a long-running search.
    """
    client = _DummyClient("google-search")

    assert client.should_send_ping() is True

    client._in_flight_calls = 1
    assert client.has_in_flight_calls is True
    assert client.should_send_ping() is False

    client._in_flight_calls = 0
    assert client.should_send_ping() is True


def test_in_flight_counter_tracks_real_tool_calls():
    """The counter must be incremented/decremented around a real call."""

    class _Session:
        async def call_tool(self, name: str, arguments: Any) -> Any:
            # While the call is executing the client must report in-flight.
            assert client.has_in_flight_calls is True
            return type("R", (), {"content": [], "isError": False})()

    client = _DummyClient("server")
    client.session = _Session()
    client._started = True

    async def run() -> None:
        await client.call_tool("search", {"q": "x"}, timeout=5)
        # Must be fully decremented afterwards.
        assert client.has_in_flight_calls is False

    asyncio.run(run())


def test_in_flight_counter_decremented_even_when_call_raises():
    """A failing call must not leak the in-flight count (would disable pings)."""

    class _Session:
        async def call_tool(self, name: str, arguments: Any) -> Any:
            raise ValueError("boom")

    client = _DummyClient("server")
    client.session = _Session()
    client._started = True

    async def run() -> None:
        with pytest.raises(ValueError):
            await client.call_tool("search", {"q": "x"}, timeout=5)
        assert client.has_in_flight_calls is False

    asyncio.run(run())
