"""One policy for client-defined vs MCP-owned tool calls.

Both the streaming and non-streaming loops use these helpers, so they cannot
drift: client tools are merged into the offered list, client names win on a
clash, and a call to a client tool ends the loop as a standard OpenAI
``tool_calls`` passthrough.
"""

from __future__ import annotations

from typing import Any


def client_tool_names(request: Any) -> set[str]:
    return set(getattr(request, "_client_tool_names", set()) or set())


def split_tool_calls(
    request: Any, calls: list[tuple[str, str]]
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Return ``(client_calls, mcp_calls)``."""
    names = client_tool_names(request)
    client_calls = [call for call in calls if call[0] in names]
    mcp_calls = [call for call in calls if call[0] not in names]
    return client_calls, mcp_calls


__all__ = ["client_tool_names", "split_tool_calls"]
