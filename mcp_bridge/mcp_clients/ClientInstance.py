from typing import Optional
import asyncio
from loguru import logger
from mcp import ClientSession
from config import config


class ClientInstance:
    name: str
    lock: asyncio.Lock
    session: Optional[ClientSession]

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

                try:
                    while True:
                        await asyncio.sleep(10)
                        if config.logging.log_server_pings:
                            logger.debug(f"pinging session for {self.name}")

                        await session.send_ping()

                except Exception as exc:
                    logger.error(f"ping failed for {self.name}: {exc}")
                    self.session = None
                
                # TODO: handle session failure

    async def __aenter__(self):
        await self.lock.acquire()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        self.lock.release()
