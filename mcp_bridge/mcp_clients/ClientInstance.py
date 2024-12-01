from typing import Any
import asyncio
from loguru import logger

class ClientInstance:
    name: str
    server: Any  # this should be one of the mcp.client classes
    lock: asyncio.Lock

    def __init__(self, name: str, server):
        logger.log("DEBUG", f"Creating client instance for {name}")
        self.name = name
        self.server = server
        self.lock = asyncio.Lock()

    async def __aenter__(self):
        await self.lock.acquire()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        self.lock.release()
