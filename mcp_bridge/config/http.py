"""Load MCP-Bridge configuration from an HTTP(S) URL.

The loader is deliberately strict: an unreachable, slow, non-2xx, non-JSON or
non-object response is a configuration failure, not an empty config. Silently
starting with no MCP servers made misconfiguration very hard to diagnose.
"""

from __future__ import annotations

import json
import os
from typing import Any

import httpx
from loguru import logger

DEFAULT_CONFIG_HTTP_TIMEOUT_SECONDS = 10.0


def _timeout_seconds() -> float:
    raw = os.getenv("MCP_BRIDGE_CONFIG_HTTP_TIMEOUT_SECONDS")
    if raw is None:
        return DEFAULT_CONFIG_HTTP_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            f"invalid MCP_BRIDGE_CONFIG_HTTP_TIMEOUT_SECONDS value: {raw}; "
            f"using default {DEFAULT_CONFIG_HTTP_TIMEOUT_SECONDS}"
        )
        return DEFAULT_CONFIG_HTTP_TIMEOUT_SECONDS
    if value <= 0:
        logger.warning(
            f"MCP_BRIDGE_CONFIG_HTTP_TIMEOUT_SECONDS={value} must be > 0; "
            f"using default {DEFAULT_CONFIG_HTTP_TIMEOUT_SECONDS}"
        )
        return DEFAULT_CONFIG_HTTP_TIMEOUT_SECONDS
    return value


def _http_optional() -> bool:
    raw = os.getenv("MCP_BRIDGE__CONFIG__HTTP_OPTIONAL", "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _safe_target(url: str) -> str:
    """Host and path only: a query string may carry an access token."""
    parsed = httpx.URL(url)
    return f"{parsed.scheme}://{parsed.host}{parsed.path}"


def _fail(url: str, reason: str) -> dict[str, Any]:
    message = f"could not load MCP-Bridge config from {_safe_target(url)}: {reason}"
    if _http_optional():
        logger.warning(
            message
            + "; continuing without it because MCP_BRIDGE__CONFIG__HTTP_OPTIONAL=true"
        )
        return {}
    logger.error(message)
    raise SystemExit(1)


def load_config(
    url: str, *, transport: httpx.BaseTransport | None = None
) -> dict[str, Any]:
    """Fetch and validate a JSON-object config.

    ``transport`` is a test seam (``httpx.MockTransport``); production callers
    omit it and get the default network transport.
    """
    timeout = _timeout_seconds()
    target = str(url)

    try:
        with httpx.Client(timeout=timeout, transport=transport) as client:
            response = client.get(target)
        response.raise_for_status()
    except httpx.TimeoutException as exc:
        return _fail(target, f"timed out after {timeout:.1f}s ({type(exc).__name__})")
    except httpx.HTTPStatusError as exc:
        return _fail(target, f"returned HTTP {exc.response.status_code}")
    except httpx.HTTPError as exc:
        return _fail(target, f"request failed ({type(exc).__name__})")

    content_type = (
        response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    )
    if "json" not in content_type:
        return _fail(
            target,
            f"returned content-type '{content_type or 'missing'}' instead of JSON",
        )

    try:
        payload = response.json()
    except (json.JSONDecodeError, ValueError):
        return _fail(target, "returned invalid JSON")

    if not isinstance(payload, dict):
        return _fail(target, "returned JSON that is not an object")

    return payload
