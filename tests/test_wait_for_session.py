import asyncio
import time

import pytest

from mcp_bridge.mcp_clients.AbstractClient import GenericMcpClient


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
