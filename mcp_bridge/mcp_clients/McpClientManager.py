from typing import Any
from config import settings
from mcp import StdioServerParameters
from .ClientInstance import ClientInstance
from .StdioClientFactory import construct_stdio_server
from loguru import logger

class MCPClientManager:
    clients: dict[str, ClientInstance] = {}

    async def initialize(self):
        logger.log("DEBUG", "Initializing MCP Client Manager")
        for server_name, server_config in settings.mcp_servers.items():
            self.clients[server_name] = await self.construct_client(server_config)

    async def construct_client(self, server_config) -> ClientInstance:
        logger.log("DEBUG", f"Constructing client for {server_config}")
        if isinstance(server_config, StdioServerParameters):
            return await construct_stdio_server(server_config)
        else:
            raise NotImplementedError("Only StdioServerParameters are supported for now")
    
    def get_client(self, server_name: str):
        return self.clients[server_name]
    
    def get_clients(self):
        return self.clients
    
ClientManager = MCPClientManager()