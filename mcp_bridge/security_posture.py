"""Startup checks for the bridge's network and authentication posture.

The historical defaults (bind ``0.0.0.0``, authentication off) are kept for
backward compatibility, but they expose tool-calling endpoints - including
filesystem and shell-capable stdio servers - to every interface. This module
makes that risk loud and gives operators a way to make it fatal.
"""

from __future__ import annotations

from loguru import logger

from mcp_bridge.config import config

_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


def is_loopback_host(host: str | None) -> bool:
    """True only for addresses that cannot be reached from another machine.

    Wildcards such as ``0.0.0.0`` and ``::`` are deliberately *not* loopback:
    they accept connections on every interface. An empty host is also not
    loopback, because its exposure is unknown.
    """
    normalized = (host or "").strip().lower().strip("[]")
    if normalized in _LOOPBACK_HOSTS:
        return True
    return normalized.startswith("127.")


def enforce_security_posture() -> None:
    """Warn about, or refuse, an unauthenticated non-loopback bind.

    When ``security.auth.enabled`` is false and the bind address is not
    loopback, log a loud warning. If
    ``security.auth.required_for_non_loopback`` is true, raise instead so the
    process fails fast: a setting whose only effect is a different log line
    would not be enforcement.
    """
    auth = config.security.auth
    host = config.network.host
    if auth.enabled or is_loopback_host(host):
        return

    message = (
        "SECURITY WARNING: authentication is disabled and MCP-Bridge is "
        f"listening on non-loopback host '{host}'. Anyone who can reach this "
        "port can call MCP tools, including filesystem and shell-capable "
        "servers. Enable security.auth, bind to 127.0.0.1, or set "
        "security.auth.required_for_non_loopback=true to refuse startup."
    )
    if getattr(auth, "required_for_non_loopback", False):
        raise RuntimeError(message)
    logger.warning(message)
