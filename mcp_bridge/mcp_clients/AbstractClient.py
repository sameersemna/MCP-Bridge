import asyncio
from abc import ABC, abstractmethod
from typing import Any

import httpx
from fastapi import HTTPException
from loguru import logger
from pydantic import AnyUrl

try:
    from mcp import McpError
    from mcp.types import (
        CallToolResult,
        ListToolsResult,
        TextContent,
        ListResourcesResult,
        ListPromptsResult,
        GetPromptResult,
        TextResourceContents,
        BlobResourceContents,
    )
except ImportError:  # pragma: no cover - allows minimal environments to import
    class McpError(RuntimeError):
        pass

    class CallToolResult:  # type: ignore[no-redef]
        def __init__(self, content: Any = None, isError: bool = False) -> None:
            self.content = content
            self.isError = isError

    class ListToolsResult:  # type: ignore[no-redef]
        def __init__(self, tools: Any = None) -> None:
            self.tools = tools or []

    class TextContent:  # type: ignore[no-redef]
        def __init__(self, type: str, text: str) -> None:
            self.type = type
            self.text = text

    class ListResourcesResult:  # type: ignore[no-redef]
        def __init__(self, resources: Any = None) -> None:
            self.resources = resources or []

    class ListPromptsResult:  # type: ignore[no-redef]
        def __init__(self, prompts: Any = None) -> None:
            self.prompts = prompts or []

    class GetPromptResult:  # type: ignore[no-redef]
        pass

    class TextResourceContents:  # type: ignore[no-redef]
        pass

    class BlobResourceContents:  # type: ignore[no-redef]
        pass

from mcp_bridge.mcp_clients.session import McpClientSession
from mcp_bridge.models.mcpServerStatus import McpServerStatus

DEFAULT_MCP_TIMEOUT_SECONDS = 30.0
DEFAULT_MCP_SESSION_TIMEOUT_SECONDS = 30


class GenericMcpClient(ABC):
    name: str
    config: Any
    client: Any
    session: McpClientSession | None = None
    _start_lock: asyncio.Lock
    _session_lock: asyncio.Lock
    _started: bool
    _maintainer_task: asyncio.Task[None] | None

    def __init__(self, name: str) -> None:
        super().__init__()
        self.session = None
        self.name = name
        self._start_lock = asyncio.Lock()
        self._session_lock = asyncio.Lock()
        self._started = False
        self._maintainer_task = None

        logger.debug(f"initializing client class for {name}")

    @abstractmethod
    async def _maintain_session(self):
        pass

    @staticmethod
    def _is_transport_error(exc: Exception) -> bool:
        if isinstance(exc, ExceptionGroup):
            return any(GenericMcpClient._is_transport_error(item) for item in exc.exceptions)

        if isinstance(exc, (httpx.HTTPStatusError, httpx.ConnectError, httpx.ReadTimeout, httpx.WriteError)):
            return True

        if isinstance(exc, TimeoutError):
            return True

        return exc.__class__.__name__ in {"HTTPStatusError", "ConnectError", "ReadTimeout", "WriteError"}

    async def _session_maintainer(self):
        reconnect_delay = 0.5
        while True:
            try:
                await self._maintain_session()
            except FileNotFoundError as e:
                logger.error(f"failed to maintain session for {self.name}: file {e.filename} not found.")
            except Exception as e:
                if self._is_transport_error(e):
                    logger.warning(f"transport error for {self.name}: {e}; leaving client offline")
                    self.session = None
                    return

                logger.error(f"failed to maintain session for {self.name}: {type(e)} {e.args}")
                if self.session is None:
                    logger.warning(f"{self.name} never established a session; leaving client offline")
                    self.session = None
                    return

            self.session = None
            logger.debug(f"restarting session for {self.name} in {reconnect_delay}s")
            await asyncio.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, 5.0)

    async def start(self) -> None:
        async with self._start_lock:
            if self._started:
                return

            self._started = True
            self._maintainer_task = asyncio.create_task(self._session_maintainer())

    async def call_tool(
        self, name: str, arguments: dict[str, Any] | None, timeout: int | None = None
    ) -> CallToolResult:
        await self._wait_for_session()

        if timeout is None:
            timeout = int(DEFAULT_MCP_TIMEOUT_SECONDS)

        normalized_arguments = arguments or {}
        if not isinstance(normalized_arguments, dict):
            raise HTTPException(status_code=400, detail="Tool arguments must be a JSON object")

        try:
            async with asyncio.timeout(timeout):
                async with self._session_lock:
                    session = self.session
                    if session is None:
                        raise RuntimeError("MCP session is not ready")
                    return await session.call_tool(
                        name=name,
                        arguments=normalized_arguments,
                    )

        except asyncio.TimeoutError:
            logger.error(f"timed out calling tool: {name}")
            return CallToolResult(
                content=[
                    TextContent(type="text", text=f"Timeout Error calling {name}")
                ],
                isError=True,
            )

        except McpError as e:
            logger.error(f"error calling {name}: {e}")
            return CallToolResult(
                content=[TextContent(type="text", text=f"Error calling {name}: {e}")],
                isError=True,
            )

    async def get_prompt(
        self, prompt: str, arguments: dict[str, str] | None
    ) -> GetPromptResult | None:
        await self._wait_for_session()

        normalized_arguments = arguments or {}
        if not isinstance(normalized_arguments, dict):
            raise HTTPException(status_code=400, detail="Prompt arguments must be a JSON object")

        try:
            async with self._session_lock:
                session = self.session
                if session is None:
                    return None
                return await session.get_prompt(prompt, normalized_arguments)
        except Exception as e:
            logger.error(f"error evaluating prompt: {e}")

        return None

    async def read_resource(
        self, uri: AnyUrl
    ) -> list[TextResourceContents | BlobResourceContents]:
        await self._wait_for_session()
        try:
            async with self._session_lock:
                session = self.session
                if session is None:
                    return []
                resource = await session.read_resource(uri)
                return resource.contents
        except Exception as e:
            logger.error(f"error reading resource: {e}")
            return []

    async def list_tools(self) -> ListToolsResult:
        # if session is None, then the client is not running
        # wait to see if it restarts
        await self._wait_for_session()

        try:
            async with self._session_lock:
                session = self.session
                if session is None:
                    return ListToolsResult(tools=[])
                return await session.list_tools()
        except Exception as e:
            logger.error(f"error listing tools: {e}")
            return ListToolsResult(tools=[])

    async def list_resources(self) -> ListResourcesResult:
        await self._wait_for_session()
        try:
            async with self._session_lock:
                session = self.session
                if session is None:
                    return ListResourcesResult(resources=[])
                return await session.list_resources()
        except Exception as e:
            logger.error(f"error listing resources: {e}")
            return ListResourcesResult(resources=[])

    async def list_prompts(self) -> ListPromptsResult:
        await self._wait_for_session()
        try:
            async with self._session_lock:
                session = self.session
                if session is None:
                    return ListPromptsResult(prompts=[])
                return await session.list_prompts()
        except Exception as e:
            logger.error(f"error listing prompts: {e}")
            return ListPromptsResult(prompts=[])

    async def _wait_for_session(self, timeout: int | None = None, http_error: bool = True):
        effective_timeout = timeout if timeout is not None else DEFAULT_MCP_SESSION_TIMEOUT_SECONDS
        try:
            async with asyncio.timeout(effective_timeout):
                while self.session is None:
                    await asyncio.sleep(1)
                    logger.debug(f"waiting for session for {self.name}")

        except asyncio.TimeoutError:
            if http_error:
                raise HTTPException(
                    status_code=500, detail=f"Could not connect to MCP server \"{self.name}\"." 
                )

            raise TimeoutError(f"Could not connect to MCP server \"{self.name}\"." )

        assert self.session is not None, "Session is None"

    async def status(self) -> McpServerStatus:
        """Get the status of the MCP server"""
        return McpServerStatus(
            name=self.name, online=self.session is not None, enabled=True
        )
