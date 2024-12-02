from typing import Any
from config import settings
from mcp import StdioServerParameters
from mcp_clients.StdioClientFactory import construct_stdio_client
from .ClientInstance import ClientInstance
from loguru import logger


class MCPClientManager:
    clients: dict[str, ClientInstance] = {}

    async def initialize(self):
        logger.log("DEBUG", "Initializing MCP Client Manager")
        for server_name, server_config in settings.mcp_servers.items():
            self.clients[server_name] = await self.construct_client(
                server_name, server_config
            )
            await self.clients[server_name].start() # TODO: make these sessions start async?

    async def construct_client(self, name, server_config) -> ClientInstance:
        logger.log("DEBUG", f"Constructing client for {server_config}")

        if isinstance(server_config, StdioServerParameters):
            client = await construct_stdio_client(server_config)

        else:
            raise NotImplementedError(
                "Only StdioServerParameters are supported for now"
            )

        return ClientInstance(name, client)

    def get_client(self, server_name: str):
        return self.clients[server_name]

    def get_clients(self):
        return list(self.clients.items())


ClientManager = MCPClientManager()
