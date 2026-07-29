from fastapi import Request
from lmos_openai_types import (
    CreateChatCompletionRequest,
    CreateChatCompletionResponse,
    ChatCompletionRequestMessage,
)

from .utils import call_tools, chat_completion_add_tools
from .genericHttpxClient import get_client
from mcp_bridge.mcp_clients.McpClientManager import ClientManager
from mcp_bridge.tool_mappers import mcp2openai
from mcp_bridge.logging import RequestTraceLogger
from loguru import logger
import json


async def chat_completions(
    request: CreateChatCompletionRequest,
    http_request: Request,
    trace_logger: RequestTraceLogger | None = None,
) -> CreateChatCompletionResponse:
    """performs a chat completion using the inference server"""

    request = await chat_completion_add_tools(request)
    if trace_logger is not None:
        trace_logger.record("tools_discovered", tools=[tool.model_dump(exclude_defaults=True, exclude_none=True, exclude_unset=True) for tool in request.tools])

    while True:
        # logger.debug(request.model_dump_json())
        async with get_client(http_request) as client:
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
        if response.choices[0].finish_reason.value in ["stop", "length"]:
            logger.debug("no tool calls found")
            return response

        logger.debug("tool calls found")
        tool_call_items = [
            (
                tool_call.function.name,
                tool_call.function.arguments,
            )
            for tool_call in response.choices[0].message.tool_calls.root
            if getattr(tool_call.function, "name", None) is not None
        ]

        if tool_call_items:
            tool_call_results = await call_tools(tool_call_items)
            if trace_logger is not None:
                trace_logger.record("mcp_tool_calls", tool_calls=[{"name": name, "arguments": arguments} for name, arguments in tool_call_items])
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

                tools_content = [
                    {"type": "text", "text": part.text}
                    for part in filter(lambda x: getattr(x, "type", None) == "text", tool_call_result.content)
                ]
                if len(tools_content) == 0:
                    tools_content = [
                        {"type": "text", "text": "the tool call result is empty"}
                    ]
                request.messages.append(
                    ChatCompletionRequestMessage.model_validate(
                        {
                            "role": "tool",
                            "content": tools_content,
                            "tool_call_id": tool_call.id,
                        }
                    )
                )

                logger.debug("sending next iteration of chat completion request")
