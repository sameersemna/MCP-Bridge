from httpx import AsyncClient, AsyncHTTPTransport
from mcp_bridge.config import config
from fastapi import Request
from contextlib import asynccontextmanager

# A single process-lifetime connection pool shared by every per-request client.
# This enables HTTP keep-alive / connection reuse against the inference server
# (the most latency-sensitive hop), instead of opening a fresh TCP connection
# for every chat-completion and tool dispatch. Each per-request `AsyncClient`
# keeps its own headers (so per-request header forwarding is isolated), but
# shares the underlying transport, so closing a client does not tear down the
# pool.
_shared_transport: AsyncHTTPTransport | None = None


def _get_shared_transport() -> AsyncHTTPTransport:
    global _shared_transport
    if _shared_transport is None:
        _shared_transport = AsyncHTTPTransport()
    return _shared_transport


async def create_client(request: Request | None = None):
    """Creates a new client instance with the appropriate headers.

    The client shares a process-lifetime connection pool (see
    ``_get_shared_transport``) but owns its own headers, so per-request header
    forwarding does not leak across concurrent requests.
    """
    client = AsyncClient(
        base_url=config.inference_server.base_url,
        headers={
            "Authorization": f"Bearer {config.inference_server.api_key}",
            "Content-Type": "application/json"
        },
        timeout=10000,
        transport=_get_shared_transport(),
    )

    if request:
        # Forward Open WebUI identity headers from the incoming request.
        headers = {k.lower(): v for k, v in request.headers.items()}

        openwebui_headers = [
            "x-openwebui-user-name",
            "x-openwebui-user-id",
            "x-openwebui-user-email",
            "x-openwebui-user-role"
        ]

        for header in openwebui_headers:
            if header in headers:
                client.headers[header] = headers[header]

    return client

@asynccontextmanager
async def get_client(request: Request | None = None):
    """Context manager for HTTP client"""
    client = await create_client(request)
    try:
        yield client
    finally:
        # Closing the client releases its per-request resources but leaves the
        # shared connection pool intact for reuse by the next request.
        await client.aclose()
