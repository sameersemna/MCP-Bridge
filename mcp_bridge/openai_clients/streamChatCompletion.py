import json
from typing import Any, Optional

import httpx
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

from .utils import call_tools, chat_completion_add_tools, sanitize_tool_result_content, tool_names
from .chatCompletion import (
    _contains_pseudo_tool_call_markers,
    _parse_pseudo_tool_calls,
)
from mcp_bridge.models import SSEData
from .genericHttpxClient import get_client
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

    except HTTPException:
        raise
    except Exception as e:
        logger.error(e)
        raise HTTPException(
            status_code=502,
            detail=f"Failed to start streaming chat completion: {e}",
        ) from e


async def chat_completions(request: CreateChatCompletionRequest, http_request: Request, trace_logger: RequestTraceLogger | None = None):
    """performs a chat completion using the inference server"""

    request.stream = True

    request = await chat_completion_add_tools(request)
    discovered_tool_names = tool_names(request.tools)
    logger.info(f"tools discovered: {len(discovered_tool_names)} available: {', '.join(discovered_tool_names)}")
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
            try:
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
                            error_text = error_data.decode(event_source.response.encoding or "utf-8")
                            logger.error(f"Response Data: {error_text}")
                            # By this point sse_starlette has already committed the
                            # HTTP response (200 OK / text/event-stream) to the
                            # client -- raising here can no longer change the
                            # status code the client sees, it can only abort the
                            # connection, producing a noisy unhandled "Exception in
                            # ASGI application" stack trace even though the
                            # failure was already fully diagnosed above. Forward
                            # the upstream's own error body to the client as a
                            # single SSE chunk instead (observed cases are already
                            # an OpenAI-style `{"error": {...}}` payload) and end
                            # the stream cleanly, so the caller gets a parseable
                            # error instead of a dropped connection plus a
                            # server-side-only stack trace.
                            yield error_text
                            yield ServerSentEvent(event="message", data="[DONE]", id=None, retry=None)
                            return

                    # iterate over the SSE stream
                    #
                    # Wrapped in try/except: if the downstream client disconnects
                    # mid-stream (browser tab closed, request timed out on their
                    # end), the cancellation can surface here as the upstream
                    # httpx connection being torn out from under us
                    # (httpx.ReadError/RemoteProtocolError) rather than a clean
                    # CancelledError. That's an expected, harmless event -- the
                    # client is simply gone -- not an application bug, so it's
                    # logged plainly and the generator ends instead of letting an
                    # "Exception in ASGI application" stack trace escape.
                    try:
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
                                logger.log("DEBUGIMP", "inference serverstream done")
                                break

                            # `data` is the raw JSON string from the SSE event, not yet
                            # parsed. Parse it ourselves so we can normalize
                            # finish_reason before validation -- some providers (e.g.
                            # OpenRouter) send it uppercase, which the strict
                            # lowercase-only enum on CreateChatCompletionStreamResponse
                            # rejects.
                            try:
                                parsed_json = json.loads(data)
                            except (json.JSONDecodeError, TypeError):
                                parsed_json = None

                            if parsed_json is not None:
                                try:
                                    parsed_json["choices"][0]["finish_reason"] = (
                                        parsed_json["choices"][0]["finish_reason"].lower()
                                    )
                                except (KeyError, IndexError, AttributeError, TypeError):
                                    pass

                            try:
                                if parsed_json is not None:
                                    parsed_data = CreateChatCompletionStreamResponse.model_validate(parsed_json)
                                else:
                                    parsed_data = CreateChatCompletionStreamResponse.model_validate_json(data)
                            except Exception as e:
                                logger.log("DEBUGIMP", "failed to parse streamed chunk; falling back to error")
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
                    except (httpx.ReadError, httpx.RemoteProtocolError) as e:
                        logger.info(
                            "streaming connection to upstream dropped "
                            f"(client likely disconnected): {type(e).__name__}: {e}"
                        )
                        return
            except httpx.TransportError as e:
                # Establishing the SSE connection to the upstream inference
                # server itself failed (e.g. the connection was reset before
                # any response headers arrived). By this point sse_starlette
                # has already committed our own 200/text-event-stream response
                # to the client, so raising here can no longer change the
                # status code seen -- it can only abort the connection with a
                # noisy unhandled "Exception in ASGI application" stack trace.
                # Yield a clean SSE error chunk instead.
                logger.warning(f"failed to connect to upstream inference server: {type(e).__name__}: {e}")
                yield json.dumps({"error": {"message": f"mcp-bridge: upstream connection failed before a response was received ({type(e).__name__}: {e})", "code": 502}})
                yield ServerSentEvent(event="message", data="[DONE]", id=None, retry=None)
                return

        # The upstream stream must have produced at least one parsed chunk. If
        # it did not (empty stream / no parseable events), fail gracefully
        # instead of raising an AssertionError (which would surface as a 500
        # and also vanish entirely under `python -O`).
        #
        # Both failures below are surfaced the same way as the Content-Type
        # mismatch above: by this point in the generator, sse_starlette has
        # already committed the HTTP response to the client, so raising an
        # HTTPException can no longer change the status code seen -- it only
        # aborts the connection with a noisy unhandled-exception stack trace.
        # Yielding a parseable error chunk plus a clean `[DONE]` gives the
        # caller an actual signal instead.
        if last is None:
            logger.error("upstream stream produced no parseable chunks")
            yield json.dumps({"error": {"message": "Upstream stream produced no parseable chunks", "code": 502}})
            yield ServerSentEvent(event="message", data="[DONE]", id=None, retry=None)
            return
        if len(last.choices) > 0 and last.choices[0].finish_reason is None:
            logger.error("upstream stream ended without a finish_reason")
            yield json.dumps({"error": {"message": "Upstream stream ended without a finish_reason", "code": 502}})
            yield ServerSentEvent(event="message", data="[DONE]", id=None, retry=None)
            return

        if len(last.choices) > 0 and last.choices[0].finish_reason.value in ["stop", "length"]:
            # The model may have emitted Anthropic-style pseudo tool-call
            # markers as plain text content (e.g. <invoke name="...">) instead
            # of structured tool_calls. Parse them so the tool still executes.
            if _contains_pseudo_tool_call_markers(response_content):
                parsed_calls = _parse_pseudo_tool_calls(response_content)
                if parsed_calls:
                    logger.warning(
                        f"model emitted pseudo tool-call markers as plain text; "
                        f"parsing {len(parsed_calls)} tool call(s) for execution"
                    )
                    collected_tool_calls = [
                        {
                            "id": f"pseudo-call-{index}",
                            "name": name,
                            "arguments": arguments,
                        }
                        for index, (name, arguments) in enumerate(parsed_calls)
                    ]
                    if trace_logger is not None:
                        trace_logger.record(
                            "pseudo_tool_calls_parsed",
                            tool_calls=[{"name": name, "arguments": arguments} for name, arguments in parsed_calls],
                        )
                else:
                    logger.log(
                        "DEBUGIMP",
                        "no tool calls found "
                        f"(finish_reason={last.choices[0].finish_reason.value}; "
                        f"{len(discovered_tool_names)} tool(s) were available to the model)"
                    )
                    fully_done = True
                    continue
            else:
                logger.log(
                    "DEBUGIMP",
                    "no tool calls found "
                    f"(finish_reason={last.choices[0].finish_reason.value}; "
                    f"{len(discovered_tool_names)} tool(s) were available to the model)"
                )
                fully_done = True
                continue

        # The model may have advertised a tool_calls finish reason but emitted
        # Anthropic-style pseudo markers as plain text content with no
        # structured tool_calls. Parse them so the tools still execute.
        if not collected_tool_calls and _contains_pseudo_tool_call_markers(response_content):
            parsed_calls = _parse_pseudo_tool_calls(response_content)
            if parsed_calls:
                logger.warning(
                    f"model returned tool_calls finish reason with pseudo markers; "
                    f"parsing {len(parsed_calls)} tool call(s) for execution"
                )
                collected_tool_calls = [
                    {
                        "id": f"pseudo-call-{index}",
                        "name": name,
                        "arguments": arguments,
                    }
                    for index, (name, arguments) in enumerate(parsed_calls)
                ]
                if trace_logger is not None:
                    trace_logger.record(
                        "pseudo_tool_calls_parsed",
                        tool_calls=[{"name": name, "arguments": arguments} for name, arguments in parsed_calls],
                    )

        logger.log(
            "DEBUGIMP",
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
                # The tool call was never dispatched (e.g. an empty/missing
                # name from a malformed streaming tool-call delta merge, or
                # any other reason `call_tool` bails out early). The
                # assistant message appended above already lists this call's
                # id in its `tool_calls`, so it still needs a matching `tool`
                # reply -- otherwise the next request has a `tool_calls`
                # message with no reply for this id, which strict providers
                # reject outright with "tool call result does not follow
                # tool call" (the same failure class this project hit and
                # fixed for the non-streaming loop; the streaming loop has
                # its own separate implementation and needed the same fix).
                call_id = tool_call.get("id", "")
                if call_id:
                    request.messages.append(
                        ChatCompletionRequestMessage.model_validate(
                            {
                                "role": "tool",
                                "content": "[not executed: tool call was not dispatched (missing or invalid tool name)]",
                                "tool_call_id": call_id,
                            }
                        )
                    )
                continue

            if trace_logger is not None:
                trace_logger.record(
                    "mcp_tool_result",
                    tool_name=tool_call.get("name", ""),
                    result=tool_call_result.model_dump(exclude_defaults=True, exclude_none=True, exclude_unset=True) if tool_call_result is not None else None,
                )

            logger.log(
                "DEBUGIMP",
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

        logger.log("DEBUGIMP", "sending next iteration of chat completion request")

    # when done, send the final event
    logger.log("DEBUGIMP", "sending final event")
    yield ServerSentEvent(event="message", data="[DONE]", id=None, retry=None)
