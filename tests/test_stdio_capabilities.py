"""The stdio client must not advertise capabilities it does not implement.

P0-07 follow-up: sampling is handled, `roots/list` is not, so the roots
capability must be omitted.
"""

from __future__ import annotations

from mcp_bridge.mcp_clients.session import stdio_client_capabilities


def test_stdio_capabilities_only_advertise_sampling() -> None:
    capabilities = stdio_client_capabilities()

    assert capabilities.sampling is not None
    assert capabilities.roots is None
