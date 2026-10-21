import asyncio
import contextlib
from contextlib import asynccontextmanager
from mcp_bridge.mcp_clients.McpClientManager import ClientManager
from mcp_bridge.openai_clients.genericHttpxClient import close_shared_transport
from mcp_bridge.retention import retention_loop
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
    retention_task = asyncio.create_task(retention_loop())
    try:
        yield
    finally:
        retention_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await retention_task
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
