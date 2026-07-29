import asyncio
import contextlib
from datetime import timedelta
from typing import Any

from loguru import logger

try:
    from mcp import McpError
    from mcp.client.sse import sse_client
    import mcp.types as types
except ImportError:  # pragma: no cover - allows the package to import in minimal environments
    class McpError(RuntimeError):
        pass

    async def sse_client(*args: Any, **kwargs: Any):
        raise RuntimeError("mcp SDK is not installed")

    types = Any

from mcp_bridge.config import config
from mcp_bridge.config.final import SSEMCPServer
from mcp_bridge.mcp_clients.session import McpClientSession
from .AbstractClient import GenericMcpClient


class SseMcpSession:
    def __init__(self, read_stream: Any, write_stream: Any, read_timeout_seconds: float | None = None) -> None:
        self._read_stream = read_stream
        self._write_stream = write_stream
        self._read_timeout_seconds = read_timeout_seconds
        self._pending_responses: dict[int, asyncio.Future[Any]] = {}
        self._request_id = 1
        self._message_task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> "SseMcpSession":
        self._message_task = asyncio.create_task(self._message_loop())
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if self._message_task is not None:
            self._message_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._message_task

    async def _message_loop(self) -> None:
        while True:
            message = await self._read_stream.receive()
            if isinstance(message, Exception):
                logger.error(f"SSE stream error: {message}")
                continue

            root = getattr(message, "root", None)
            if root is None:
                continue

            if isinstance(root, types.JSONRPCResponse):
                request_id = getattr(root, "id", None)
                if request_id in self._pending_responses:
                    self._pending_responses.pop(request_id).set_result(root)
            elif isinstance(root, types.JSONRPCError):
                request_id = getattr(root, "id", None)
                if request_id in self._pending_responses:
                    self._pending_responses.pop(request_id).set_exception(McpError(root.error))
            elif isinstance(root, types.JSONRPCNotification):
                logger.debug(f"received notification from SSE server: {root}")
            elif isinstance(root, types.JSONRPCRequest):
                logger.debug(f"received request from SSE server: {root}")

    async def initialize(self) -> Any:
        response = await self._send_request(
            "initialize",
            {
                "protocolVersion": getattr(types, "LATEST_PROTOCOL_VERSION", "2024-11-05"),
                "capabilities": {
                    "sampling": {},
                    "roots": {"listChanged": True},
                },
                "clientInfo": {"name": "MCP-Bridge", "version": "0.5.1"},
            },
            result_type=types.InitializeResult,
        )
        await self._send_notification("notifications/initialized", None)
        return response

    async def send_ping(self) -> Any:
        return await self._send_request("ping", None, result_type=types.EmptyResult)

    async def list_tools(self) -> Any:
        return await self._send_request("tools/list", None, result_type=types.ListToolsResult)

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        return await self._send_request(
            "tools/call",
            {"name": name, "arguments": arguments},
            result_type=types.CallToolResult,
        )

    async def _send_request(self, method: str, params: Any, result_type: Any) -> Any:
        request_id = self._request_id
        self._request_id += 1
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending_responses[request_id] = future

        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        }
        await self._write_stream.send(types.JSONRPCMessage(types.JSONRPCRequest(**payload)))

        try:
            timeout_seconds = self._read_timeout_seconds
            timeout = None if timeout_seconds is None else float(timeout_seconds)
            if timeout is None:
                response = await future
            else:
                response = await asyncio.wait_for(future, timeout=timeout)
        finally:
            self._pending_responses.pop(request_id, None)

        if not hasattr(response, "result"):
            raise McpError("Invalid response payload")
        return result_type.model_validate(response.result)

    async def _send_notification(self, method: str, params: Any) -> None:
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        }
        await self._write_stream.send(types.JSONRPCMessage(types.JSONRPCNotification(**payload)))


class SseClient(GenericMcpClient):
    config: SSEMCPServer

    def __init__(self, name: str, config: SSEMCPServer) -> None:
        super().__init__(name=name)

        self.config = config

    async def _maintain_session(self) -> None:
        async with sse_client(self.config.url) as client:
            async with SseMcpSession(*client, read_timeout_seconds=self.config.requestTimeout / 1000.0 if self.config.requestTimeout else None) as session:
                await session.initialize()
                logger.debug(f"finished initialise session for {self.name}")
                self.session = session

                try:
                    while True:
                        await asyncio.sleep(10)
                        if config.logging.log_server_pings:
                            logger.debug(f"pinging session for {self.name}")

                        await session.send_ping()

                except Exception as exc:
                    logger.error(f"ping failed for {self.name}: {exc}")
                    self.session = None
                    raise

        logger.debug(f"exiting session for {self.name}")
