from fastapi import APIRouter, HTTPException, Request

from lmos_openai_types import CreateChatCompletionRequest, CreateCompletionRequest
from opentelemetry import trace

from mcp_bridge.openai_clients import (
    get_client,
    completions,
    chat_completions,
    streaming_chat_completions,
)

from mcp_bridge.openapi_tags import Tag
from mcp_bridge.logging import RequestTraceLogger
import json

router = APIRouter(prefix="/v1", tags=[Tag.openai])
tracer = trace.get_tracer("mcp_bridge.endpoints")


@router.post("/completions")
async def openai_completions(
    request: CreateCompletionRequest, 
    http_request: Request
):
    """Completions endpoint"""
    if request.stream:
        raise NotImplementedError("Streaming Completion is not supported")
    else:
        return await completions(request, http_request)


@router.post("/chat/completions")
async def openai_chat_completions(
    request: CreateChatCompletionRequest, 
    http_request: Request
):
    """Chat Completions endpoint"""
    with tracer.start_as_current_span("openai.chat.completions") as span:
        span.set_attribute("http.method", http_request.method)
        span.set_attribute("http.route", http_request.url.path)
        span.set_attribute("mcp_bridge.request.stream", bool(request.stream))
        span.set_attribute("mcp_bridge.request.model", getattr(request, "model", "") or "")
        span.set_attribute("mcp_bridge.request.tool_count", len(getattr(request, "tools", []) or []))
        span.set_attribute(
            "mcp_bridge.request.preview",
            json.dumps(
                request.model_dump(exclude_defaults=True, exclude_none=True, exclude_unset=True),
                ensure_ascii=False,
                default=str,
            )[:1600],
        )

        trace_logger = RequestTraceLogger(
            request_payload=request.model_dump(exclude_defaults=True, exclude_none=True, exclude_unset=True),
            http_path=http_request.url.path,
            method=http_request.method,
        )
        trace_logger.record("incoming_request", prompt=request.model_dump(exclude_defaults=True, exclude_none=True, exclude_unset=True))
        if request.stream:
            response = await streaming_chat_completions(request, http_request, trace_logger)
        else:
            response = await chat_completions(request, http_request, trace_logger)

        if response is None:
            # Defense in depth: no code path should produce a null response, but
            # guard against it so clients never see an HTTP 200 with a null body.
            trace_logger.record("outgoing_response", response=None)
            raise HTTPException(status_code=502, detail="Chat completion produced no response")

        if not request.stream:
            span.set_attribute(
                "mcp_bridge.response.preview",
                json.dumps(
                    response.model_dump(exclude_defaults=True, exclude_none=True, exclude_unset=True),
                    ensure_ascii=False,
                    default=str,
                )[:1600],
            )
            trace_logger.record("outgoing_response", response=response.model_dump(exclude_defaults=True, exclude_none=True, exclude_unset=True))
        return response


@router.get("/models")
async def models(request: Request):
    """List models"""
    async with get_client(request) as client:
        response = await client.get("/models")
    return response.json()
