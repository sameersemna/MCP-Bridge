import json
from typing import Optional
from lmos_openai_types import (
    ChatCompletionRequestMessage,
    CreateChatCompletionRequest,
    CreateChatCompletionStreamResponse,
)
from .utils import chat_completion_add_tools
from models import SSEData
from .genericHttpxClient import client
from mcp_clients.McpClientManager import ClientManager
from tool_mappers import mcp2openai
from loguru import logger
from httpx_sse import aconnect_sse

from sse_starlette.sse import EventSourceResponse, ServerSentEvent


async def streaming_chat_completions(request: CreateChatCompletionRequest):
    # raise NotImplementedError("Streaming Chat Completion is not supported")

    try:
        return EventSourceResponse(
            content=chat_completions(request),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache"},
        )

    except Exception as e:
        logger.error(e)


async def chat_completions(request: CreateChatCompletionRequest):
    """performs a chat completion using the inference server"""

    request.stream = True

    request = await chat_completion_add_tools(request)

    fully_done = False
    while not fully_done:
        json_data = request.model_dump_json(
            exclude_defaults=True, exclude_none=True, exclude_unset=True
        )

        logger.debug(json_data)

        last: Optional[CreateChatCompletionStreamResponse] = None  # last message

        tool_call_name: str = ""
        tool_call_json: str = ""
        should_forward: bool = True

        async with aconnect_sse(
            client, "post", "/chat/completions", content=json_data
        ) as event_source:
            async for sse in event_source.aiter_sse():
                event = sse.event
                data = sse.data
                id = sse.id
                retry = sse.retry

                logger.debug(
                    f"event: {event},\ndata: {data},\nid: {id},\nretry: {retry}"
                )

                # handle if the SSE stream is done
                if data == "[DONE]":
                    break

                parsed_data = CreateChatCompletionStreamResponse.model_validate_json(
                    data
                )

                if parsed_data.choices[0].finish_reason is not None:
                    if parsed_data.choices[0].finish_reason.value in [
                        "stop",
                        "length",
                    ]:
                        fully_done = True
                    else:
                        should_forward = False

                # this manages the incoming tool call schema
                # most of this is assertions to please mypy
                if parsed_data.choices[0].delta.tool_calls is not None:
                    should_forward = False
                    assert parsed_data.choices[0].delta.tool_calls[0].function is not None

                    name = parsed_data.choices[0].delta.tool_calls[0].function.name
                    name = name if name is not None else ""
                    tool_call_name = name if tool_call_name == "" else tool_call_name

                    arg = parsed_data.choices[0].delta.tool_calls[0].function.arguments
                    tool_call_json += arg if arg is not None else ""

                logger.debug(f"{should_forward=}")
                if should_forward:
                    # we do not want to forward tool call json to the client
                    logger.debug("forwarding message")
                    yield SSEData.model_validate_json(sse.data).model_dump_json()

                last = parsed_data

        # ideally we should check this properly
        assert last is not None
        assert last.choices[0].finish_reason is not None

        if last.choices[0].finish_reason.value in ["stop", "length"]:
            logger.debug("no tool calls found")
            fully_done = True

        logger.debug("tool calls found")
        logger.error(f"{tool_call_name=} {tool_call_json=}") # this should not be error but its easier to debug 
        break # FIXME: this is a hack to break out of the loop

    # when done, send the final event
    logger.debug("sending final event")
    yield ServerSentEvent(event="message", data="[DONE]", id=None, retry=None)
