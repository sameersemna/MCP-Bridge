from httpx import AsyncBaseTransport, AsyncClient, AsyncHTTPTransport, Request, Response, Timeout
from mcp_bridge.config import config
from fastapi import Request as FastApiRequest
from contextlib import asynccontextmanager
import os

# A single process-lifetime connection pool shared by every per-request client.
# This enables HTTP keep-alive / connection reuse against the inference server
# (the most latency-sensitive hop), instead of opening a fresh TCP connection
# for every chat-completion and tool dispatch. Each per-request `AsyncClient`
# keeps its own headers (so per-request header forwarding is isolated) and is
# handed a proxy transport, because httpx 0.28's `AsyncClient.aclose()` closes
# whatever transport it was given. The real transport is closed once, by the
# FastAPI lifespan, through `close_shared_transport()`.
_shared_transport: AsyncHTTPTransport | None = None
_shared_transport_proxy: "_LifespanOwnedTransport | None" = None

# Upstream LLM request timeouts (seconds). httpx timeout values are in SECONDS,
# not milliseconds. The previous hardcoded `timeout=10000` was interpreted as
# 10000 seconds (~2.8 hours), which caused a long research run to hang for
# ~35 minutes on a dead final-turn request before the retry logic kicked in.
# These are now configurable via env vars with sane defaults.
DEFAULT_UPSTREAM_CONNECT_TIMEOUT_SECONDS = 10.0
DEFAULT_UPSTREAM_READ_TIMEOUT_SECONDS = 300.0
DEFAULT_UPSTREAM_WRITE_TIMEOUT_SECONDS = 10.0
DEFAULT_UPSTREAM_POOL_TIMEOUT_SECONDS = 10.0


def _get_upstream_timeout() -> Timeout:
    def _env_float(name: str, default: float) -> float:
        raw = os.getenv(name)
        if raw is None:
            return default
        try:
            value = float(raw)
            return value if value > 0 else default
        except ValueError:
            return default

    return Timeout(
        connect=_env_float("MCP_BRIDGE_UPSTREAM_CONNECT_TIMEOUT_SECONDS", DEFAULT_UPSTREAM_CONNECT_TIMEOUT_SECONDS),
        read=_env_float("MCP_BRIDGE_UPSTREAM_READ_TIMEOUT_SECONDS", DEFAULT_UPSTREAM_READ_TIMEOUT_SECONDS),
        write=_env_float("MCP_BRIDGE_UPSTREAM_WRITE_TIMEOUT_SECONDS", DEFAULT_UPSTREAM_WRITE_TIMEOUT_SECONDS),
        pool=_env_float("MCP_BRIDGE_UPSTREAM_POOL_TIMEOUT_SECONDS", DEFAULT_UPSTREAM_POOL_TIMEOUT_SECONDS),
    )


def _get_shared_transport() -> "_LifespanOwnedTransport":
    """Return the shared transport proxy, creating the pool on first use."""
    global _shared_transport, _shared_transport_proxy
    if _shared_transport is None:
        _shared_transport = AsyncHTTPTransport()
    # Rebuild the proxy if the underlying transport was replaced (tests do
    # this, and close_shared_transport resets both globals).
    if _shared_transport_proxy is None or _shared_transport_proxy._transport is not _shared_transport:
        _shared_transport_proxy = _LifespanOwnedTransport(_shared_transport)
    return _shared_transport_proxy


class _LifespanOwnedTransport(AsyncBaseTransport):
    """Forward to the shared pool but refuse to close it.

    ``AsyncClient.aclose()`` unconditionally closes its transport, so a
    per-request client given the shared transport used to destroy the process
    pool after every request. This proxy forwards requests and makes ``aclose``
    a no-op; ``close_shared_transport`` owns the real shutdown.
    """

    def __init__(self, transport: AsyncHTTPTransport) -> None:
        self._transport = transport

    async def handle_async_request(self, request: Request) -> Response:
        return await self._transport.handle_async_request(request)

    async def aclose(self) -> None:
        # The FastAPI lifespan owns the pool's lifetime.
        return None


async def close_shared_transport() -> None:
    """Close the process-wide upstream pool exactly once.

    Called from the FastAPI lifespan on shutdown. Resetting the globals also
    makes a subsequent startup create a fresh pool instead of reusing a closed
    one.
    """
    global _shared_transport, _shared_transport_proxy
    transport = _shared_transport
    _shared_transport = None
    _shared_transport_proxy = None
    if transport is not None:
        await transport.aclose()


async def create_client(request: FastApiRequest | None = None):
    """Creates a new client instance with the appropriate headers.

    The client is handed a proxy around the process-lifetime connection pool
    (see ``_get_shared_transport``) but owns its own headers, so per-request
    header forwarding does not leak across concurrent requests.
    """
    client = AsyncClient(
        base_url=config.inference_server.base_url,
        headers={
            "Authorization": f"Bearer {config.inference_server.api_key}",
            "Content-Type": "application/json"
        },
        timeout=_get_upstream_timeout(),
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
async def get_client(request: FastApiRequest | None = None):
    """Context manager for HTTP client"""
    client = await create_client(request)
    try:
        yield client
    finally:
        # Closing the client releases its per-request resources. The proxy
        # transport makes this a no-op for the shared pool, which the lifespan
        # closes through close_shared_transport().
        await client.aclose()
