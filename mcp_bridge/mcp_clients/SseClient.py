import asyncio
from typing import Any

from loguru import logger

try:
    from mcp.client.sse import sse_client
except ImportError:  # pragma: no cover - allows the package to import in minimal environments
    async def sse_client(*args: Any, **kwargs: Any):
        raise RuntimeError("mcp SDK is not installed")

from mcp_bridge.config import config
from mcp_bridge.config.final import SSEMCPServer
from mcp_bridge.mcp_clients.session import McpClientSession
from .AbstractClient import GenericMcpClient


class SseClient(GenericMcpClient):
    config: SSEMCPServer

    def __init__(self, name: str, config: SSEMCPServer) -> None:
        super().__init__(name=name)

        self.config = config

    async def _maintain_session(self) -> None:
        async with sse_client(self.config.url) as client:
            async with McpClientSession(*client) as session:
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
