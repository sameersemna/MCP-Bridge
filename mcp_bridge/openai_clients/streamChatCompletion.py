import json
from typing import Any, Optional
from fastapi import HTTPException, Request

try:
    from lmos_openai_types import (
        ChatCompletionMessageToolCall,
        ChatCompletionRequestMessage,
        CreateChatCompletionRequest,
        CreateChatCompletionStreamResponse,
        Function1,
    )
except ImportError:  # pragma: no cover - fallback for minimal environments
    from pydantic import BaseModel, Field

    class Function1(BaseModel):
        name: str = ""
        arguments: str = ""

    class ChatCompletionMessageToolCall(BaseModel):
        id: str = ""
        type: str = "function"
        function: Function1 = Field(default_factory=Function1)

    class ChatCompletionRequestMessage(BaseModel):
        role: str
        content: str | None = None
        tool_calls: list[ChatCompletionMessageToolCall] | None = None
        tool_call_id: str | None = None

    class CreateChatCompletionRequest(BaseModel):
        stream: bool = False
        messages: list[ChatCompletionRequestMessage] = Field(default_factory=list)
        tools: list[Any] = Field(default_factory=list)

    class FinishReason(BaseModel):
        value: str | None = None

    class StreamDelta(BaseModel):
        content: str | None = None
        tool_calls: list[ChatCompletionMessageToolCall] | None = None

    class StreamChoice(BaseModel):
        delta: StreamDelta = Field(default_factory=StreamDelta)
        finish_reason: FinishReason | None = None

    class CreateChatCompletionStreamResponse(BaseModel):
        choices: list[StreamChoice] = Field(default_factory=list)

from .utils import call_tools, chat_completion_add_tools, sanitize_tool_result_content
from mcp_bridge.models import SSEData
from .genericHttpxClient import get_client
from mcp_bridge.mcp_clients.McpClientManager import ClientManager
from mcp_bridge.tool_mappers import mcp2openai
from mcp_bridge.logging import RequestTraceLogger
from loguru import logger

try:
    from httpx_sse import aconnect_sse
except ImportError:  # pragma: no cover - fallback for minimal environments
    async def aconnect_sse(*args: Any, **kwargs: Any):
        raise RuntimeError("httpx_sse is not installed")

try:
    from sse_starlette.sse import EventSourceResponse, ServerSentEvent
except ImportError:  # pragma: no cover - fallback for minimal environments
    class EventSourceResponse:  # type: ignore[no-redef]
        def __init__(self, *args: Any, **kwargs: Any):
            self.args = args
            self.kwargs = kwargs

    class ServerSentEvent:  # type: ignore[no-redef]
        def __init__(self, event: str = "message", data: str = "", id: str | None = None, retry: int | None = None):
            self.event = event
            self.data = data
            self.id = id
            self.retry = retry


def merge_streaming_tool_calls(
    existing_calls: list[dict[str, str]],
    deltas: list[Any],
) -> list[dict[str, str]]:
    """Merge partial streamed tool-call deltas into a single ordered list."""
    merged = list(existing_calls)

    for delta in deltas or []:
        index = getattr(delta, "index", None)
        if index is None:
            index = len(merged)

        while len(merged) <= index:
            merged.append({"id": "", "name": "", "arguments": ""})

        entry = merged[index]
        entry["id"] = entry.get("id", "") or getattr(delta, "id", "") or ""

        function = getattr(delta, "function", None)
        if function is None:
            continue

        name = getattr(function, "name", None)
        if name:
            entry["name"] = name

        arguments = getattr(function, "arguments", None)
        if arguments:
            entry["arguments"] += arguments

    return merged


async def streaming_chat_completions(request: CreateChatCompletionRequest, http_request: Request, trace_logger: RequestTraceLogger | None = None):
    # raise NotImplementedError("Streaming Chat Completion is not supported")

    try:
        return EventSourceResponse(
            content=chat_completions(request, http_request, trace_logger),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache"},
        )

    except Exception as e:
        logger.error(e)


async def chat_completions(request: CreateChatCompletionRequest, http_request: Request, trace_logger: RequestTraceLogger | None = None):
    """performs a chat completion using the inference server"""

    request.stream = True

    request = await chat_completion_add_tools(request)
    if trace_logger is not None:
        trace_logger.record("tools_discovered", tools=[tool.model_dump(exclude_defaults=True, exclude_none=True, exclude_unset=True) for tool in request.tools])

    fully_done = False
    while not fully_done:
        # json_data = request.model_dump_json(
        #     exclude_defaults=True, exclude_none=True, exclude_unset=True
        # )

        json_data = json.dumps(request.model_dump(
            exclude_defaults=True, exclude_none=True, exclude_unset=True
        ))

        # logger.debug(json_data)

        last: Optional[CreateChatCompletionStreamResponse] = None  # last message

        should_forward: bool = True
        response_content: str = ""
        collected_tool_calls: list[dict[str, str]] = []

        async with get_client(http_request) as client:
            async with aconnect_sse(
                client, "post", "/chat/completions", content=json_data
            ) as event_source:
                
                # check if the content type is correct because the aiter_sse method
                # will raise an exception if the content type is not correct
                if "Content-Type" in event_source.response.headers:
                    content_type = event_source.response.headers["Content-Type"]
                    if "text/event-stream" not in content_type:
                        logger.error(f"Unexpected Content-Type: {content_type}")
                        error_data = await event_source.response.aread()
                        logger.error(f"Request URL: {event_source.response.url}")
                        logger.error(f"Request Data: {json_data}")
                        logger.error(f"Response Status: {event_source.response.status_code}")
                        logger.error(f"Response Data: {error_data.decode(event_source.response.encoding or 'utf-8')}")
                        raise HTTPException(status_code=500, detail="Unexpected Content-Type")

                # iterate over the SSE stream
                async for sse in event_source.aiter_sse():
                    event = sse.event
                    data = sse.data
                    id = sse.id
                    retry = sse.retry

                    logger.debug(
                        "stream event received: "
                        f"event={event}; id={id}; retry={retry}; data_len={len(data or '')}"
                    )

                    # handle if the SSE stream is done
                    if data == "[DONE]":
                        logger.debug("inference serverstream done")
                        break

                    # for some reason openrouter uses uppercase for finish_reason
                    try:
                        data['choices'][0]['finish_reason'] = data['choices'][0]['finish_reason'].lower() # type: ignore
                    except Exception as e:
                        logger.debug(f"failed to lowercase finish_reason: {e}")

                    try:
                        parsed_data = CreateChatCompletionStreamResponse.model_validate_json(
                            data
                        )
                    except Exception as e:
                        logger.debug("failed to parse streamed chunk; falling back to error")
                        raise e

                    # add the delta to the response content
                    content = parsed_data.choices[0].delta.content if len(parsed_data.choices) > 0 else ""
                    content = content if content is not None else ""
                    response_content += content

                    # handle stop reasons
                    if  len(parsed_data.choices) > 0 and parsed_data.choices[0].finish_reason is not None:
                        if parsed_data.choices[0].finish_reason.value in [
                            "stop",
                            "length",
                        ]:
                            fully_done = True
                        else:
                            should_forward = False

                    # this manages the incoming tool call schema
                    if len(parsed_data.choices) > 0 and parsed_data.choices[0].delta.tool_calls is not None:
                        should_forward = False
                        collected_tool_calls = merge_streaming_tool_calls(
                            collected_tool_calls,
                            parsed_data.choices[0].delta.tool_calls,
                        )

                    # forward SSE messages to the client
                    logger.debug(f"{should_forward=}")
                    if should_forward:
                        # we do not want to forward tool call json to the client
                        logger.debug("forwarding message")
                        yield SSEData.model_validate_json(sse.data).model_dump_json()

                    # save the last message
                    last = parsed_data

        # ideally we should check this properly
        assert last is not None
        if len(last.choices) > 0:
            assert last.choices[0].finish_reason is not None

        if len(last.choices) > 0 and last.choices[0].finish_reason.value in ["stop", "length"]:
            logger.debug("no tool calls found")
            fully_done = True
            continue

        logger.debug(
            "tool calls found in stream; "
            f"count={len(collected_tool_calls)}"
        )

        # add received message to the history
        msg = ChatCompletionRequestMessage(
            role="assistant",
            content=response_content,
            tool_calls=[
                ChatCompletionMessageToolCall(
                    id=tool_call.get("id", ""),
                    type="function",
                    function=Function1(
                        name=tool_call.get("name", ""),
                        arguments=tool_call.get("arguments", ""),
                    ),
                )
                for tool_call in collected_tool_calls
            ],
        )  # type: ignore
        request.messages.append(msg)
        if trace_logger is not None:
            trace_logger.record("assistant_message", message=msg.model_dump(exclude_defaults=True, exclude_none=True, exclude_unset=True))

        #### MOST OF THIS IS COPY PASTED FROM CHAT_COMPLETIONS
        if not collected_tool_calls:
            continue

        if trace_logger is not None:
            trace_logger.record("mcp_tool_calls", tool_calls=[{"name": tool_call.get("name", ""), "arguments": tool_call.get("arguments", "")} for tool_call in collected_tool_calls])

        tool_call_results = await call_tools(
            [(tool_call.get("name", ""), tool_call.get("arguments", "")) for tool_call in collected_tool_calls],
            trace_logger=trace_logger,
        )

        for tool_call, tool_call_result in zip(collected_tool_calls, tool_call_results):
            if tool_call_result is None:
                continue

            if trace_logger is not None:
                trace_logger.record(
                    "mcp_tool_result",
                    tool_name=tool_call.get("name", ""),
                    result=tool_call_result.model_dump(exclude_defaults=True, exclude_none=True, exclude_unset=True) if tool_call_result is not None else None,
                )

            logger.debug(
                f"tool call result for {tool_call.get('name', '')}: {len(getattr(tool_call_result, 'content', []) or [])} content part(s), isError={getattr(tool_call_result, 'isError', False)}"
            )

            if getattr(tool_call_result, 'content', None):
                preview_text = str(tool_call_result.content)
                preview_text = " ".join(preview_text.split())
                if len(preview_text) > 400:
                    preview_text = preview_text[:397].rstrip() + "…"
                logger.debug(f"tool call result content preview: {preview_text}")

            tools_content = sanitize_tool_result_content(
                tool_call.get("name", ""),
                tool_call_result,
            )
            request.messages.append(
                ChatCompletionRequestMessage.model_validate(
                    {
                        "role": "tool",
                        "content": tools_content,
                        "tool_call_id": tool_call.get("id", ""),
                    }
                )
            )
            if trace_logger is not None:
                trace_logger.record(
                    "tool_message",
                    tool_name=tool_call.get("name", ""),
                    tool_result=tool_call_result.model_dump(exclude_defaults=True, exclude_none=True, exclude_unset=True),
                )

        logger.debug("sending next iteration of chat completion request")

    # when done, send the final event
    logger.debug("sending final event")
    yield ServerSentEvent(event="message", data="[DONE]", id=None, retry=None)
