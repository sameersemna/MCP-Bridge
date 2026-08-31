import asyncio
import time

import anyio
import pytest
import mcp.types as types

from mcp_bridge.mcp_clients.AbstractClient import GenericMcpClient
from mcp_bridge.mcp_clients.StdioClient import StdioClient
from mcp_bridge.mcp_clients.session import McpClientSession


class DummyClient(GenericMcpClient):
    async def _maintain_session(self) -> None:
        return None


class BlockingClient(GenericMcpClient):
    async def _maintain_session(self) -> None:
        await asyncio.sleep(10)


def test_wait_for_session_emits_single_timeout_warning(monkeypatch):
    client = DummyClient("dummy")
    messages: list[str] = []

    monkeypatch.setattr(
        "mcp_bridge.mcp_clients.AbstractClient.logger.warning",
        lambda message: messages.append(message),
    )

    async def run_test() -> None:
        with pytest.raises(TimeoutError):
            await client._wait_for_session(
                timeout=0.05,
                http_error=False,
                log_interval=5.0,
                poll_interval=0.01,
            )

    asyncio.run(run_test())

    assert len(messages) == 1


def test_client_stop_cleans_up_maintainer_task():
    client = BlockingClient("blocking")

    async def run_test() -> None:
        await client.start()
        await asyncio.sleep(0.05)
        await client.stop()
        await asyncio.sleep(0.05)
        assert client._maintainer_task is None or client._maintainer_task.done()

    asyncio.run(run_test())


def test_wait_for_session_returns_quickly_for_offline_client(monkeypatch):
    client = DummyClient("dummy")

    async def raise_timeout(*args, **kwargs):
        raise TimeoutError("offline")

    monkeypatch.setattr(client, "_wait_for_session", raise_timeout)

    async def run_test() -> None:
        start = time.perf_counter()
        with pytest.raises(TimeoutError):
            await client._wait_for_session(timeout=0.05, http_error=False)
        elapsed = time.perf_counter() - start
        assert elapsed < 0.2

    asyncio.run(run_test())


def test_stdio_maintainer_keeps_session_available_without_ping(monkeypatch):
    class DummyStdioConfig:
        def __init__(self):
            self.command = "echo"
            self.args = []
            self.env = None
            self.encoding_error_handler = "ignore"
            self.model_fields_set = set()
            self.requestTimeout = None

        def model_copy(self, deep=True):
            return DummyStdioConfig()

    client = StdioClient("dummy", DummyStdioConfig())

    class FakeSession:
        def __init__(self):
            self.initialized = False
            self.ping_count = 0

        async def initialize(self):
            self.initialized = True

        async def send_ping(self):
            self.ping_count += 1

    fake_session = FakeSession()

    async def fake_context_manager(*args, **kwargs):
        yield None

    async def fake_maintain_session():
        await fake_session.initialize()
        client.session = fake_session
        await asyncio.sleep(0.05)

    monkeypatch.setattr("mcp_bridge.mcp_clients.StdioClient.stdio_client", fake_context_manager)
    monkeypatch.setattr(client, "_maintain_session", fake_maintain_session)

    async def run_test():
        await client.start()
        await asyncio.sleep(0.1)
        await client.stop()

    asyncio.run(run_test())
    assert fake_session.initialized is True
    assert getattr(client.session, "ping_count", 0) == 0


def test_session_exit_does_not_close_underlying_streams():
    async def run_test():
        read_stream_writer, read_stream = anyio.create_memory_object_stream(10)
        write_stream, _ = anyio.create_memory_object_stream(10)
        session = McpClientSession(read_stream, write_stream)

        await session.__aenter__()
        await session.__aexit__(None, None, None)

        await read_stream_writer.send("message")
        received = await read_stream.receive()
        assert received == "message"

    asyncio.run(run_test())


def test_session_handles_notification_before_response():
    async def run_test():
        read_stream_writer, read_stream = anyio.create_memory_object_stream(10)
        write_stream, _ = anyio.create_memory_object_stream(10)
        session = McpClientSession(read_stream, write_stream)

        async def feed_messages():
            await read_stream_writer.send(
                types.JSONRPCMessage(
                    types.JSONRPCNotification(
                        jsonrpc="2.0",
                        method="notifications/message",
                        params={"level": "info", "data": "hello"},
                    )
                )
            )
            await read_stream_writer.send(
                types.JSONRPCMessage(
                    types.JSONRPCResponse(jsonrpc="2.0", id=0, result={})
                )
            )
            await read_stream_writer.aclose()

        await session.__aenter__()
        try:
            async with anyio.create_task_group() as tg:
                tg.start_soon(feed_messages)
                response = await asyncio.wait_for(
                    session.send_request(
                        types.ClientRequest(types.PingRequest(method="ping")),
                        types.EmptyResult,
                    ),
                    timeout=1,
                )
                assert response == types.EmptyResult()
        finally:
            await session.__aexit__(None, None, None)

    asyncio.run(run_test())
