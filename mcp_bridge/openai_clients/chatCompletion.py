import os
import time
from typing import Any

from fastapi import Request
from lmos_openai_types import (
    CreateChatCompletionRequest,
    CreateChatCompletionResponse,
    ChatCompletionRequestMessage,
)

from .utils import call_tools, chat_completion_add_tools, sanitize_tool_result_content
from .genericHttpxClient import get_client
from mcp_bridge.mcp_clients.McpClientManager import ClientManager
from mcp_bridge.tool_mappers import mcp2openai
from mcp_bridge.logging import RequestTraceLogger
from loguru import logger
import json

DEFAULT_MAX_TOOL_TURNS = 12
DEFAULT_TOOL_TIMEOUT_SECONDS = 60


def get_tool_timeout_seconds() -> int:
    raw_value = os.getenv("MCP_BRIDGE_TOOL_TIMEOUT_SECONDS")
    if raw_value is None:
        return DEFAULT_TOOL_TIMEOUT_SECONDS

    try:
        return int(raw_value)
    except ValueError:
        logger.warning(f"invalid MCP_BRIDGE_TOOL_TIMEOUT_SECONDS value: {raw_value}; using default {DEFAULT_TOOL_TIMEOUT_SECONDS}")
        return DEFAULT_TOOL_TIMEOUT_SECONDS


def _summarize_trace(trace_logger: RequestTraceLogger) -> dict[str, object]:
    events = trace_logger.events
    return {
        "event_count": len(events),
        "last_event_type": events[-1]["type"] if events else None,
        "tool_events": sum(1 for event in events if event["type"] in {"mcp_tool_calls", "mcp_tool_result"}),
        "llm_responses": sum(1 for event in events if event["type"] == "llm_response"),
    }


def should_continue_tool_loop(
    finish_reason: str | None,
    *,
    tool_call_count: int,
    iteration_count: int,
    max_tool_turns: int = DEFAULT_MAX_TOOL_TURNS,
) -> bool:
    if tool_call_count > 0 and iteration_count < max_tool_turns:
        return True

    if finish_reason in {"stop", "length"}:
        return False

    if finish_reason in {"tool_calls", "function_call"}:
        return tool_call_count > 0 and iteration_count < max_tool_turns

    if tool_call_count <= 0:
        return False

    return iteration_count < max_tool_turns


def _record_timing(trace_logger: RequestTraceLogger | None, stage: str, elapsed_seconds: float) -> None:
    if trace_logger is None:
        return

    trace_logger.record(
        "timing",
        stage=stage,
        elapsed_ms=round(elapsed_seconds * 1000, 2),
    )


def _format_tool_loop_stop_message(*, tool_turns_completed: int, max_tool_turns: int) -> str:
    return f"stopping tool loop after {tool_turns_completed} turn(s); max_tool_turns={max_tool_turns}"


def _build_tool_error_response(response: CreateChatCompletionResponse, tool_errors: list[str]) -> CreateChatCompletionResponse:
    error_summary = "; ".join(tool_errors)
    response.choices[0].message.content = (
        "I wasn't able to complete the request because one or more MCP tool calls failed: "
        + error_summary
    )
    response.choices[0].message.tool_calls = None
    response.choices[0].finish_reason = None
    return response


async def chat_completions(
    request: CreateChatCompletionRequest,
    http_request: Request,
    trace_logger: RequestTraceLogger | None = None,
) -> CreateChatCompletionResponse:
    """performs a chat completion using the inference server"""

    if not getattr(request, "tools", None):
        request = await chat_completion_add_tools(request)
    if trace_logger is not None:
        trace_logger.record("tools_discovered", tools=[tool.model_dump(exclude_defaults=True, exclude_none=True, exclude_unset=True) for tool in request.tools])

    max_tool_turns = int(os.getenv("MCP_BRIDGE_MAX_TOOL_TURNS", str(DEFAULT_MAX_TOOL_TURNS)))
    tool_timeout_seconds = get_tool_timeout_seconds()
    tool_turns_completed = 0
    tool_client_cache: dict[str, Any] = {}

    async with get_client(http_request) as client:
        while True:
            start_time = time.perf_counter()
            # logger.debug(request.model_dump_json())
            text = (
                await client.post(
                    "/chat/completions",
                    #content=request.model_dump_json(
                    #    exclude_defaults=True, exclude_none=True, exclude_unset=True
                    #),
                    json=request.model_dump(exclude_defaults=True, exclude_none=True, exclude_unset=True),
                )
            ).text
            logger.debug(text)
            _record_timing(trace_logger, "upstream_llm_request", time.perf_counter() - start_time)
            try:
                response = CreateChatCompletionResponse.model_validate_json(text)
                if trace_logger is not None:
                    trace_logger.record(
                        "llm_response",
                        response=response.model_dump(exclude_defaults=True, exclude_none=True, exclude_unset=True),
                    )
            except Exception as e:
                logger.error(f"Error parsing response: {text}")
                logger.error(e)
                return None

            msg = response.choices[0].message
            msg = ChatCompletionRequestMessage(
                role="assistant",
                content=msg.content,
                tool_calls=msg.tool_calls,
            )  # type: ignore
            request.messages.append(msg)

            logger.debug(f"finish reason: {response.choices[0].finish_reason}")
            if trace_logger is not None:
                trace_logger.record(
                    "assistant_message",
                    message=msg.model_dump(exclude_defaults=True, exclude_none=True, exclude_unset=True),
                )

            finish_reason_value = (
                response.choices[0].finish_reason.value
                if response.choices[0].finish_reason is not None
                else None
            )
            if finish_reason_value in ["stop", "length"]:
                logger.debug("no tool calls found")
                return response

            logger.debug("tool calls found")
            if trace_logger is not None:
                trace_logger.record("tool_call_decision", finish_reason=finish_reason_value)
            tool_call_items = [
                (
                    tool_call.function.name,
                    tool_call.function.arguments,
                )
                for tool_call in response.choices[0].message.tool_calls.root
                if getattr(tool_call.function, "name", None) is not None
            ]

            if not tool_call_items:
                logger.warning("model returned a tool-like finish reason without tool calls; stopping loop")
                return response

            if not should_continue_tool_loop(
                finish_reason_value,
                tool_call_count=len(tool_call_items),
                iteration_count=tool_turns_completed,
                max_tool_turns=max_tool_turns,
            ):
                logger.warning(
                    _format_tool_loop_stop_message(
                        tool_turns_completed=tool_turns_completed,
                        max_tool_turns=max_tool_turns,
                    )
                )
                return response

            tool_turns_completed += 1

            if tool_call_items:
                tool_call_results = await call_tools(
                    tool_call_items,
                    timeout=tool_timeout_seconds,
                    trace_logger=trace_logger,
                    client_cache=tool_client_cache,
                )
                if trace_logger is not None:
                    trace_logger.record("mcp_tool_calls", tool_calls=[{"name": name, "arguments": arguments} for name, arguments in tool_call_items])

                tool_errors: list[str] = []
                for tool_call, tool_call_result in zip(
                    response.choices[0].message.tool_calls.root,
                    tool_call_results,
                ):
                    if tool_call_result is None:
                        logger.warning(
                            f"tool call '{getattr(tool_call.function, 'name', 'unknown')}' returned no result"
                        )
                        continue

                    logger.debug(
                        f"tool call result for {tool_call.function.name}: {tool_call_result.model_dump()}"
                    )
                    if trace_logger is not None:
                        trace_logger.record(
                            "mcp_tool_result",
                            tool_name=tool_call.function.name,
                            result=tool_call_result.model_dump(exclude_defaults=True, exclude_none=True, exclude_unset=True),
                        )

                    logger.debug(f"tool call result content: {tool_call_result.content}")

                    if getattr(tool_call_result, "isError", False):
                        error_text = next(
                            (
                                part.text
                                for part in getattr(tool_call_result, "content", [])
                                if getattr(part, "type", None) == "text"
                            ),
                            "tool call failed",
                        )
                        tool_errors.append(f"{tool_call.function.name}: {error_text}")

                    tools_content = sanitize_tool_result_content(
                        tool_call.function.name,
                        tool_call_result,
                    )
                    request.messages.append(
                        ChatCompletionRequestMessage.model_validate(
                            {
                                "role": "tool",
                                "content": tools_content,
                                "tool_call_id": tool_call.id,
                            }
                        )
                    )

                    if trace_logger is not None:
                        trace_logger.record(
                            "tool_message",
                            tool_name=tool_call.function.name,
                            tool_result=tool_call_result.model_dump(exclude_defaults=True, exclude_none=True, exclude_unset=True),
                        )

                    logger.debug("sending next iteration of chat completion request")

                if tool_errors:
                    logger.warning(
                        f"tool call failures detected; stopping tool loop: {'; '.join(tool_errors)}"
                    )
                    return _build_tool_error_response(response, tool_errors)
