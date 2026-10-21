from typing import Any
from fastapi import APIRouter, Body, Header, HTTPException, Request, Response
from mcp_bridge.idempotency import (
    get_idempotency_store,
    make_fingerprint,
    request_identity,
)
from mcp_bridge.metrics import metrics
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
    tool_name: str,
    response: Response,
    http_request: Request,
    arguments: dict[str, Any] = Body(default_factory=dict),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> CallToolResult:
    """Call a tool.

    Supports the ``Idempotency-Key`` header: a completed request replays its
    stored result, an in-progress duplicate gets 409, and reusing the key with a
    different body gets 422.
    """
    store = None
    fingerprint = ""
    if idempotency_key:
        store = get_idempotency_store()
        fingerprint = make_fingerprint(
            identity=request_identity(http_request),
            method=http_request.method,
            path=http_request.url.path,
            body={"arguments": arguments},
        )
        state, entry = await store.begin(idempotency_key, fingerprint)
        if state == "mismatch":
            raise HTTPException(
                status_code=422,
                detail="Idempotency-Key was reused with a different request body",
            )
        if state == "in_progress":
            raise HTTPException(
                status_code=409,
                detail="A request with this Idempotency-Key is still in progress",
            )
        if state == "replay":
            if entry is None or entry.body is None:
                raise HTTPException(
                    status_code=500,
                    detail="Stored idempotent response is no longer available",
                )
            response.headers["Idempotent-Replayed"] = "true"
            metrics.increment(
                "mcp_bridge_idempotency_replays_total", {"endpoint": "tool"}
            )
            return CallToolResult.model_validate(entry.body)

    try:
        resolved = await ClientManager.resolve_tool(tool_name)
        if resolved is None:
            raise HTTPException(status_code=404, detail=f"Tool '{tool_name}' not found")
        client, resolved_name = resolved
        result = await client.call_tool(resolved_name, arguments)
    except Exception:
        if store is not None and idempotency_key:
            await store.release(idempotency_key)
        raise

    if store is not None and idempotency_key:
        body = result.model_dump(exclude_none=True)
        await store.complete(
            idempotency_key,
            status=200,
            body=body,
            body_bytes=len(str(body).encode("utf-8")),
        )
    return result
