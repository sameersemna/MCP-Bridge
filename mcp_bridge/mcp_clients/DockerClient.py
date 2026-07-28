import asyncio
from typing import Any

from loguru import logger

try:
    from mcpx.client.transports.docker import docker_client, DockerMCPServer
except ImportError:  # pragma: no cover - allows the package to import in minimal environments
    class DockerMCPServer:  # type: ignore[no-redef]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.args = args
            self.kwargs = kwargs

    async def docker_client(*args: Any, **kwargs: Any):
        raise RuntimeError("mcpx SDK is not installed")

from mcp_bridge.mcp_clients.session import McpClientSession
from mcp_bridge.config import config
from .AbstractClient import GenericMcpClient


class DockerClient(GenericMcpClient):
    config: DockerMCPServer

    def __init__(self, name: str, config: DockerMCPServer) -> None:
        super().__init__(name=name)

        self.config = config

    async def _maintain_session(self) -> None:
        async with docker_client(self.config) as client:
            logger.debug(f"made instance of docker client for {self.name}")
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
