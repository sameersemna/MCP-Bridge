import asyncio
import inspect
import json
from typing import Any

from loguru import logger
from opentelemetry import trace

from mcp_bridge.logging import RequestTraceLogger

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


tracer = trace.get_tracer("mcp_bridge.openai_clients.utils")


async def call_tool(
    tool_call_name: str, tool_call_json: str, timeout: int | None = None,
    trace_logger: RequestTraceLogger | None = None,
) -> Any | None:
    with tracer.start_as_current_span("mcp_bridge.call_tool") as span:
        span.set_attribute("mcp_bridge.tool.name", tool_call_name or "")
        span.set_attribute("mcp_bridge.tool.arguments.length", len(tool_call_json or ""))

        if tool_call_name == "" or tool_call_name is None:
            logger.error("tool call name is empty")
            if trace_logger is not None:
                trace_logger.record(
                    "mcp_tool_dispatch_result",
                    tool_name=tool_call_name or "<empty>",
                    is_error=True,
                    reason="empty_tool_name",
                )
            return None

        if tool_call_json is None:
            logger.error("tool call json is empty")
            if trace_logger is not None:
                trace_logger.record(
                    "mcp_tool_dispatch_result",
                    tool_name=tool_call_name,
                    is_error=True,
                    reason="empty_tool_arguments",
                )
            return None

        if trace_logger is not None:
            trace_logger.record("mcp_tool_dispatch_attempt", tool_name=tool_call_name, arguments=tool_call_json)

        session = await ClientManager.get_client_from_tool(
            tool_call_name, timeout=DEFAULT_MCP_SESSION_TIMEOUT_SECONDS
        )

        if session is None:
            logger.error(f"no MCP client found for tool '{tool_call_name}'")
            span.set_status(trace.Status(trace.StatusCode.ERROR, "no_mcp_client"))
            if trace_logger is not None:
                trace_logger.record(
                    "mcp_tool_dispatch_result",
                    tool_name=tool_call_name,
                    is_error=True,
                    reason="no_mcp_client",
                )
            return None

        try:
            tool_call_args = json.loads(tool_call_json)
        except json.JSONDecodeError:
            logger.error(f"failed to decode json for {tool_call_name}")
            span.set_status(trace.Status(trace.StatusCode.ERROR, "invalid_json"))
            if trace_logger is not None:
                trace_logger.record(
                    "mcp_tool_dispatch_result",
                    tool_name=tool_call_name,
                    is_error=True,
                    reason="invalid_json",
                )
            return None

        try:
            result = await session.call_tool(tool_call_name, tool_call_args, timeout)
        except Exception as exc:
            logger.error(f"tool dispatch failed for {tool_call_name}: {exc}")
            span.set_status(trace.Status(trace.StatusCode.ERROR, str(exc)))
            if trace_logger is not None:
                trace_logger.record(
                    "mcp_tool_dispatch_result",
                    tool_name=tool_call_name,
                    is_error=True,
                    reason=str(exc),
                )
            return None

        span.set_attribute("mcp_bridge.tool.result.is_error", bool(getattr(result, "isError", False)))
        if trace_logger is not None:
            trace_logger.record(
                "mcp_tool_dispatch_result",
                tool_name=tool_call_name,
                is_error=getattr(result, "isError", False),
                result=result.model_dump(exclude_defaults=True, exclude_none=True, exclude_unset=True) if hasattr(result, "model_dump") else result,
            )
        return result


async def call_tools(
    tool_calls: list[tuple[str, str]], timeout: int | None = None,
    trace_logger: RequestTraceLogger | None = None,
) -> list[Any]:
    """Execute multiple tool calls concurrently while preserving order."""

    if not tool_calls:
        return []

    async def _run(call: tuple[str, str]) -> Any:
        name, payload = call
        call_kwargs = {"timeout": timeout}
        if trace_logger is not None:
            call_kwargs["trace_logger"] = trace_logger

        signature = inspect.signature(call_tool)
        if "trace_logger" in signature.parameters:
            return await call_tool(name, payload, **call_kwargs)

        return await call_tool(name, payload, timeout)

    return await asyncio.gather(*(_run(call) for call in tool_calls))
