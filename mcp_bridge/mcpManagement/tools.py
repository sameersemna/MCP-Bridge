from typing import Any
from fastapi import APIRouter, Body, HTTPException
from mcp_bridge.mcp_clients.McpClientManager import ClientManager
from mcp.types import ListToolsResult, CallToolResult

router = APIRouter(prefix="/tools")


@router.get("")
async def get_tools() -> dict[str, ListToolsResult]:
    """Get all tools from all MCP clients"""

    tools = {}

    for name, client in ClientManager.get_clients():
        tools[name] = await client.list_tools()

    return tools


@router.post("/{tool_name}/call")
async def call_tool(
    tool_name: str, arguments: dict[str, Any] = Body(default_factory=dict)
) -> CallToolResult:
    """Call a tool.

    ``Body(default_factory=dict)`` avoids the shared mutable default that made
    every call without a body reuse one dict. Dispatch uses the *resolved*
    canonical name, not the alias the caller used, so the server receives a
    name it actually knows.
    """

    resolved = await ClientManager.resolve_tool(tool_name)
    if resolved is None:
        raise HTTPException(status_code=404, detail=f"Tool '{tool_name}' not found")

    client, resolved_name = resolved
    return await client.call_tool(resolved_name, arguments)
