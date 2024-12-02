from typing import Any
import asyncio
from loguru import logger
from mcp import ClientSession


class ClientInstance:
    name: str
    lock: asyncio.Lock
    session: None

    def __init__(self, name: str, client):
        logger.log("DEBUG", f"Creating client instance for {name}")
        self.name = name
        self._client = client
        self.lock = asyncio.Lock()

    async def start(self):
        asyncio.create_task(self._maintain_session())

    async def _maintain_session(self):
        async with self._client as client:
            async with ClientSession(*client) as session:
                await session.initialize()
                logger.debug(f"finished initialise session for {self.name}")
                self.session = session
                await asyncio.Future()

    async def __aenter__(self):
        await self.lock.acquire()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        self.lock.release()
