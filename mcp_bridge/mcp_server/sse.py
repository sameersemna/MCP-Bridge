import asyncio
from anyio import BrokenResourceError
from fastapi.responses import StreamingResponse
from mcp.server.sse import SseServerTransport
from fastapi import APIRouter, Request
from loguru import logger

from .server import server, options

router = APIRouter(prefix="/sse")

sse = SseServerTransport("/mcp-server/sse")


@router.get("/", response_class=StreamingResponse)
async def handle_sse(request: Request):
    """sse endpoint for using MCP-Bridge with external clients"""

    async with sse.connect_sse(
        request.scope, request.receive, request._send
    ) as streams:
        try:
            await server.run(*streams, initialization_options=options)

        except BrokenResourceError:
            # Handle gracefully when client disconnects
            logger.info("Client disconnected from SSE connection")
        except asyncio.CancelledError:
            logger.info("SSE connection was cancelled")


@router.post("/")
async def handle_messages(request: Request):
    try:
        await sse.handle_post_message(request.scope, request.receive, request._send)
    except Exception as e:
        logger.exception(e)
        # raise e
