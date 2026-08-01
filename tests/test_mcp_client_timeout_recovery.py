import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp_bridge.mcp_clients.AbstractClient import GenericMcpClient


class DummyClient(GenericMcpClient):
    async def _maintain_session(self) -> None:
        return None


class SessionFactorySession:
    attempt_counter = 0

    def __init__(self, result: str) -> None:
        self.result = result

    async def call_tool(self, name: str, arguments: dict[str, object]):
        type(self).attempt_counter += 1
        if type(self).attempt_counter == 1:
            raise asyncio.TimeoutError()
        return type("Result", (), {"content": [type("Content", (), {"type": "text", "text": self.result})()], "isError": False})()


class RecoveringClient(DummyClient):
    async def _wait_for_session(self, timeout=None, http_error=True, log_interval=None, poll_interval=None):
        if self.session is None:
            self.session = SessionFactorySession("recovered")


def test_call_tool_rebuilds_session_after_timeout(monkeypatch):
    async def run_test() -> None:
        client = RecoveringClient("dummy")
        client.session = None

        async def fake_sleep(_: float) -> None:
            return None

        monkeypatch.setattr("mcp_bridge.mcp_clients.AbstractClient.asyncio.sleep", fake_sleep)

        result = await client.call_tool("fetch_content", {"url": "https://example.com"}, timeout=1)

        assert result.isError is False
        assert result.content[0].text == "recovered"
        assert client.session is not None
        assert SessionFactorySession.attempt_counter == 2

    asyncio.run(run_test())


def test_transient_tool_timeouts_do_not_emit_warning_logs(monkeypatch):
    async def run_test() -> None:
        client = RecoveringClient("dummy")
        client.session = None
        log_events: list[tuple[str, str]] = []

        async def fake_sleep(_: float) -> None:
            return None

        def capture_warning(message: str) -> None:
            log_events.append(("warning", message))

        def capture_info(message: str) -> None:
            log_events.append(("info", message))

        monkeypatch.setattr("mcp_bridge.mcp_clients.AbstractClient.asyncio.sleep", fake_sleep)
        monkeypatch.setattr("mcp_bridge.mcp_clients.AbstractClient.logger.warning", capture_warning)
        monkeypatch.setattr("mcp_bridge.mcp_clients.AbstractClient.logger.info", capture_info)

        result = await client.call_tool("fetch_content", {"url": "https://example.com"}, timeout=1)

        assert result.isError is False
        assert not any(level == "warning" for level, _ in log_events)

    asyncio.run(run_test())
