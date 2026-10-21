import asyncio
from contextlib import asynccontextmanager
from mcp_bridge.mcp_clients.McpClientManager import ClientManager
from mcp_bridge.openai_clients.genericHttpxClient import close_shared_transport
from mcp_bridge.telemetry import shutdown_tracing
from loguru import logger


@asynccontextmanager
async def lifespan(app):
    """Lifespan context manager for fastapi"""

    # startup
    logger.log("DEBUG", "Entered fastapi lifespan")
    await ClientManager.initialize()
    logger.log("DEBUG", "Initialized MCP Client Manager")

    logger.log("DEBUG", "Yielding lifespan")
    yield
    logger.log("DEBUG", "Returned form lifespan yield")

    # shutdown
    try:
        # Stop downstream clients first: they may still use the HTTP pool and
        # their transports need to unwind (stdio children are terminated when
        # the maintainer task is cancelled).
        await ClientManager.shutdown()
    finally:
        try:
            await close_shared_transport()
        finally:
            # Flushing spans can perform network I/O; keep it off the loop.
            await asyncio.to_thread(shutdown_tracing)

    logger.log("DEBUG", "Exiting fastapi lifespan")
