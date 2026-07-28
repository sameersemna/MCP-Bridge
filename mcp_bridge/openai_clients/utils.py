import asyncio
import json
from typing import Any

from loguru import logger

try:
    from lmos_openai_types import CreateChatCompletionRequest
except ImportError:  # pragma: no cover - optional dependency support
    CreateChatCompletionRequest = Any

try:
    import mcp.types
except ImportError:  # pragma: no cover - optional dependency support
    mcp = Any

from mcp_bridge.mcp_clients.AbstractClient import DEFAULT_MCP_SESSION_TIMEOUT_SECONDS
from mcp_bridge.mcp_clients.McpClientManager import ClientManager
from mcp_bridge.tool_mappers import mcp2openai


async def chat_completion_add_tools(request: CreateChatCompletionRequest):
    request.tools = []

    for _, session in ClientManager.get_clients():
        try:
            await session._wait_for_session(timeout=DEFAULT_MCP_SESSION_TIMEOUT_SECONDS, http_error=False)
        except Exception:
            logger.warning(f"session not ready for {session.name}; skipping tool discovery")
            continue

        if session.session is None:
            logger.error(f"session is `None` for {session.name}")
            continue

        tools = await session.session.list_tools()
        for tool in tools.tools:
            request.tools.append(mcp2openai(tool))

    return request


async def call_tool(
    tool_call_name: str, tool_call_json: str, timeout: int | None = None
) -> Any | None:
    if tool_call_name == "" or tool_call_name is None:
        logger.error("tool call name is empty")
        return None

    if tool_call_json is None:
        logger.error("tool call json is empty")
        return None

    session = await ClientManager.get_client_from_tool(
        tool_call_name, timeout=DEFAULT_MCP_SESSION_TIMEOUT_SECONDS
    )

    if session is None:
        logger.error(f"no MCP client found for tool '{tool_call_name}'")
        return None

    try:
        tool_call_args = json.loads(tool_call_json)
    except json.JSONDecodeError:
        logger.error(f"failed to decode json for {tool_call_name}")
        return None

    return await session.call_tool(tool_call_name, tool_call_args, timeout)


async def call_tools(
    tool_calls: list[tuple[str, str]], timeout: int | None = None
) -> list[Any]:
    """Execute multiple tool calls concurrently while preserving order."""

    if not tool_calls:
        return []

    async def _run(call: tuple[str, str]) -> Any:
        name, payload = call
        return await call_tool(name, payload, timeout)

    return await asyncio.gather(*(_run(call) for call in tool_calls))
