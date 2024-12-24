import asyncio
from abc import ABC, abstractmethod
from typing import Any


class GenericMcpClient(ABC):
    name: str
    config: Any
    client: Any

    def __init__(self, name: str) -> None:
        super().__init__()
        self.name = name

    @abstractmethod
    async def _maintain_session(self):
        pass

    async def start(self):
        asyncio.create_task(self._maintain_session())

    async def call_tool(self, name: str, arguments: dict) -> dict:
        raise NotImplementedError("call_tool is not implemented")

    async def list_tools(self) -> dict:
        raise NotImplementedError("list_tools is not implemented")

    async def list_resources(self) -> dict:
        raise NotImplementedError("list_resources is not implemented")
