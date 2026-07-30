import asyncio
import inspect
import json
import os
import re
from types import SimpleNamespace
from typing import Any

from loguru import logger
from opentelemetry import trace

from mcp_bridge.logging import RequestTraceLogger

try:
    from lmos_openai_types import ChatCompletionRequestMessage, CreateChatCompletionRequest
except ImportError:  # pragma: no cover - optional dependency support
    class ChatCompletionRequestMessage:  # type: ignore[no-redef]
        def __init__(self, role: str, content: Any = None, **kwargs: Any) -> None:
            self.role = role
            self.content = content
            for key, value in kwargs.items():
                setattr(self, key, value)

        @classmethod
        def model_validate(cls, payload: Any) -> "ChatCompletionRequestMessage":
            if isinstance(payload, cls):
                return payload
            if isinstance(payload, dict):
                return cls(**payload)
            return cls(role="assistant", content=str(payload))

    CreateChatCompletionRequest = Any

try:
    import mcp.types
except ImportError:  # pragma: no cover - optional dependency support
    mcp = Any

from mcp_bridge.mcp_clients.AbstractClient import DEFAULT_MCP_SESSION_TIMEOUT_SECONDS
from mcp_bridge.mcp_clients.McpClientManager import ClientManager, DEFAULT_MCP_DISCOVERY_TIMEOUT_SECONDS
from mcp_bridge.tool_mappers import mcp2openai

def maybe_add_tool_selection_instructions(request: Any) -> Any:
    tool_names: list[str] = []
    for tool in getattr(request, "tools", []) or []:
        if isinstance(tool, dict):
            function_payload = tool.get("function") or {}
            if isinstance(function_payload, dict):
                tool_name = function_payload.get("name") or tool.get("name")
            else:
                tool_name = getattr(function_payload, "name", None) or tool.get("name")
        else:
            tool_name = getattr(tool, "name", None)
            if tool_name is None and hasattr(tool, "function"):
                tool_name = getattr(getattr(tool, "function"), "name", None)
        if tool_name is not None:
            tool_names.append(str(tool_name))

    has_github_search_tool = any(name == "searchGitHub" or "github" in name.lower() for name in tool_names)
    if not has_github_search_tool:
        return request

    messages = list(getattr(request, "messages", []) or [])
    if not messages:
        return request

    text_chunks = []
    for message in messages:
        content = getattr(message, "content", None)
        if isinstance(content, list):
            text_chunks.extend(str(part) for part in content if part is not None)
        elif content is not None:
            text_chunks.append(str(content))

    combined_text = "\n".join(text_chunks).lower()
    is_code_search_prompt = any(
        phrase in combined_text
        for phrase in [
            "example",
            "code",
            "implementation",
            "repository",
            "github",
            "pattern",
            "snippet",
            "usage",
            "library",
        ]
    )
    if not is_code_search_prompt:
        return request

    if any(getattr(message, "role", None) == "system" and "searchGitHub" in str(getattr(message, "content", "")) for message in messages):
        return request

    instruction = (
        "When the user asks for implementation examples, code patterns, or real repository-based examples, "
        "prefer using the searchGitHub tool before answering from memory. "
        "Use searchGitHub for concrete code/example searches and cite the result in your answer."
    )

    system_message = SimpleNamespace(role="system", content=instruction)

    request.messages = [system_message, *messages]
    return request


def get_tool_discovery_timeout_seconds() -> float:
    raw_value = os.getenv("MCP_BRIDGE_TOOL_DISCOVERY_TIMEOUT_SECONDS")
    if raw_value is None:
        return DEFAULT_MCP_SESSION_TIMEOUT_SECONDS

    try:
        return float(raw_value)
    except ValueError:
        logger.warning(
            f"invalid MCP_BRIDGE_TOOL_DISCOVERY_TIMEOUT_SECONDS value: {raw_value}; using default {DEFAULT_MCP_SESSION_TIMEOUT_SECONDS}"
        )
        return DEFAULT_MCP_SESSION_TIMEOUT_SECONDS


async def _ensure_client_manager_initialized() -> list[tuple[str, Any]]:
    clients = ClientManager.get_clients()
    if clients:
        return clients

    logger.info("No MCP clients initialized yet; initializing client manager before tool discovery")
    await ClientManager.initialize()
    return ClientManager.get_clients()


async def chat_completion_add_tools(request: CreateChatCompletionRequest):
    request.tools = []

    tool_discovery_timeout_seconds = get_tool_discovery_timeout_seconds()
    clients = await _ensure_client_manager_initialized()

    async def _discover_tools_for_session(session: Any) -> list[Any]:
        configured_request_timeout = None
        config = getattr(session, "config", None)
        request_timeout = getattr(config, "requestTimeout", None)
        if request_timeout is not None:
            configured_request_timeout = float(request_timeout) / 1000.0

        wait_timeout = float(tool_discovery_timeout_seconds)
        if configured_request_timeout is not None:
            wait_timeout = max(wait_timeout, configured_request_timeout)

        try:
            await session._wait_for_session(timeout=wait_timeout, http_error=False)
        except Exception:
            logger.warning(f"session not ready for {session.name}; skipping tool discovery")
            return []

        if session.session is None:
            logger.error(f"session is `None` for {session.name}")
            return []

        try:
            tools = await asyncio.wait_for(session.session.list_tools(), timeout=wait_timeout)
        except Exception as exc:
            logger.warning(f"tool discovery failed for {session.name}: {exc}")
            return []

        return [mcp2openai(tool) for tool in tools.tools]

    discovered_tools = await asyncio.gather(
        *(_discover_tools_for_session(session) for _, session in clients),
        return_exceptions=False,
    )

    for tools in discovered_tools:
        request.tools.extend(tools)

    maybe_add_tool_selection_instructions(request)
    return request


DEFAULT_MAX_SEARCH_RESULTS = 5

tracer = trace.get_tracer("mcp_bridge.openai_clients.utils")


def _is_search_tool(tool_name: str | None) -> bool:
    if tool_name is None:
        return False

    normalized_name = tool_name.lower()
    return normalized_name == "search" or normalized_name in {"searchgithub", "search_web", "web_search", "websearch"}


def get_max_search_results() -> int:
    raw_value = os.getenv("MCP_BRIDGE_MAX_SEARCH_RESULTS")
    if raw_value is None:
        return DEFAULT_MAX_SEARCH_RESULTS

    try:
        return max(1, int(raw_value))
    except ValueError:
        logger.warning(f"invalid MCP_BRIDGE_MAX_SEARCH_RESULTS value: {raw_value}; using default {DEFAULT_MAX_SEARCH_RESULTS}")
        return DEFAULT_MAX_SEARCH_RESULTS


def clamp_search_tool_arguments(tool_name: str | None, arguments: Any) -> Any:
    if not isinstance(arguments, dict) or not _is_search_tool(tool_name):
        return arguments

    max_results = get_max_search_results()
    if "max_results" not in arguments:
        arguments = dict(arguments)
        arguments["max_results"] = max_results
        return arguments

    try:
        requested_max_results = int(arguments["max_results"])
    except (TypeError, ValueError):
        requested_max_results = max_results

    clamped = min(requested_max_results, max_results)
    if clamped < 1:
        clamped = 1

    updated_arguments = dict(arguments)
    updated_arguments["max_results"] = clamped
    return updated_arguments


def truncate_search_result_text(tool_name: str | None, text: str | None, max_results: int | None = None) -> str | None:
    if not isinstance(text, str) or not _is_search_tool(tool_name):
        return text

    result_limit = max_results if max_results is not None else get_max_search_results()
    if result_limit < 1:
        result_limit = 1

    pattern = re.compile(r"(?m)^\s*(\d+)\.\s")
    matches = list(pattern.finditer(text))
    if len(matches) <= result_limit:
        return text

    kept_parts: list[str] = []
    for index in range(result_limit):
        start = matches[index].start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        part = text[start:end].strip()
        if part:
            kept_parts.append(part)

    summary = "\n\n".join(kept_parts)
    return (
        f"Showing the first {result_limit} search results only; additional results were omitted to avoid overwhelming the tool loop.\n\n"
        + summary
    )


def sanitize_tool_result_content(tool_name: str | None, tool_call_result: Any, max_results: int | None = None) -> list[dict[str, str]]:
    if not hasattr(tool_call_result, "content"):
        return []

    text_parts: list[dict[str, str]] = []
    for part in getattr(tool_call_result, "content", []):
        if getattr(part, "type", None) != "text":
            continue

        original_text = getattr(part, "text", "") or ""
        sanitized_text = truncate_search_result_text(tool_name, original_text, max_results=max_results)
        if sanitized_text is None:
            sanitized_text = original_text

        text_parts.append({"type": "text", "text": sanitized_text})

    if not text_parts:
        return [{"type": "text", "text": "the tool call result is empty"}]

    return text_parts


def _span_payload_preview(payload: Any, max_len: int = 160) -> str:
    if payload is None:
        return "null"

    try:
        rendered = json.dumps(payload, ensure_ascii=False, default=str)
    except TypeError:
        rendered = str(payload)

    if len(rendered) <= max_len:
        return rendered

    return rendered[:max_len] + "...[truncated]"


async def call_tool(
    tool_call_name: str, tool_call_json: str, timeout: int | None = None,
    trace_logger: RequestTraceLogger | None = None,
    client_cache: dict[str, Any] | None = None,
) -> Any | None:
    with tracer.start_as_current_span("mcp_bridge.call_tool") as span:
        span.set_attribute("mcp_bridge.tool.name", tool_call_name or "")
        span.set_attribute("mcp_bridge.tool.arguments.length", len(tool_call_json or ""))
        span.set_attribute("mcp_bridge.tool.arguments.preview", _span_payload_preview(tool_call_json))
        span.set_attribute("mcp_bridge.tool.arguments.json_valid", bool(tool_call_json is not None))
        span.set_attribute("mcp_bridge.tool.timeout_seconds", float(timeout or 0))

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

        if client_cache is not None and tool_call_name in client_cache:
            session = client_cache[tool_call_name]
        else:
            session = await ClientManager.get_client_from_tool(
                tool_call_name, timeout=DEFAULT_MCP_DISCOVERY_TIMEOUT_SECONDS
            )
            if client_cache is not None:
                client_cache[tool_call_name] = session

        if session is None:
            logger.error(f"no MCP client found for tool '{tool_call_name}'")
            span.set_attribute("mcp_bridge.tool.client_found", False)
            span.set_status(trace.Status(trace.StatusCode.ERROR, "no_mcp_client"))
            if trace_logger is not None:
                trace_logger.record(
                    "mcp_tool_dispatch_result",
                    tool_name=tool_call_name,
                    is_error=True,
                    reason="no_mcp_client",
                )
            class ToolDispatchError:
                isError = True

                def __init__(self, message: str) -> None:
                    self.content = [type("ToolTextContent", (), {"type": "text", "text": message})()]

                def model_dump(self, **kwargs: Any) -> dict[str, Any]:
                    return {"isError": True, "content": [{"type": "text", "text": self.content[0].text}]}

            return ToolDispatchError(f"No MCP client found for tool '{tool_call_name}'")

        try:
            tool_call_args = json.loads(tool_call_json)
            span.set_attribute("mcp_bridge.tool.arguments.parsed", True)
            span.set_attribute("mcp_bridge.tool.arguments.keys", ",".join(sorted(str(key) for key in tool_call_args.keys())) if isinstance(tool_call_args, dict) else "")
        except json.JSONDecodeError:
            logger.error(f"failed to decode json for {tool_call_name}")
            span.set_attribute("mcp_bridge.tool.arguments.parsed", False)
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
            span.set_attribute("mcp_bridge.tool.client_name", getattr(session, "name", ""))
            result = await session.call_tool(tool_call_name, clamp_search_tool_arguments(tool_call_name, tool_call_args), timeout)
        except Exception as exc:
            logger.error(f"tool dispatch failed for {tool_call_name}: {exc}")
            span.set_attribute("mcp_bridge.tool.client_name", getattr(session, "name", ""))
            span.set_attribute("mcp_bridge.tool.result.is_error", True)
            span.set_status(trace.Status(trace.StatusCode.ERROR, str(exc)))
            if trace_logger is not None:
                trace_logger.record(
                    "mcp_tool_dispatch_result",
                    tool_name=tool_call_name,
                    is_error=True,
                    reason=str(exc),
                )
            return None

        span.set_attribute("mcp_bridge.tool.client_found", True)
        span.set_attribute("mcp_bridge.tool.result.is_error", bool(getattr(result, "isError", False)))
        span.set_attribute("mcp_bridge.tool.result.preview", _span_payload_preview(result.model_dump(exclude_defaults=True, exclude_none=True, exclude_unset=True) if hasattr(result, "model_dump") else result))
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
    client_cache: dict[str, Any] | None = None,
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
            call_kwargs["client_cache"] = client_cache
            return await call_tool(name, payload, **call_kwargs)

        return await call_tool(name, payload, timeout)

    return await asyncio.gather(*(_run(call) for call in tool_calls))
