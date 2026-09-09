from httpx import AsyncClient, AsyncHTTPTransport, Timeout
from mcp_bridge.config import config
from fastapi import Request
from contextlib import asynccontextmanager
import os

# A single process-lifetime connection pool shared by every per-request client.
# This enables HTTP keep-alive / connection reuse against the inference server
# (the most latency-sensitive hop), instead of opening a fresh TCP connection
# for every chat-completion and tool dispatch. Each per-request `AsyncClient`
# keeps its own headers (so per-request header forwarding is isolated), but
# shares the underlying transport, so closing a client does not tear down the
# pool.
_shared_transport: AsyncHTTPTransport | None = None

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
async def get_client(request: Request | None = None):
    """Context manager for HTTP client"""
    client = await create_client(request)
    try:
        yield client
    finally:
        # Closing the client releases its per-request resources but leaves the
        # shared connection pool intact for reuse by the next request.
        await client.aclose()
