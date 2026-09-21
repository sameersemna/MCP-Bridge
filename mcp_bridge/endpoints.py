from fastapi import APIRouter, HTTPException, Request

from lmos_openai_types import CreateChatCompletionRequest, CreateCompletionRequest
from opentelemetry import trace
from typing import Any

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


def _apply_citation_guard(request: Any, response: Any, trace_logger: RequestTraceLogger) -> None:
    """Flag fabricated/unverified citations in the final response.

    Uses the evidence URLs the tool loop attached to the request
    (``request._citation_evidence``), falling back to the request's message
    history when that is unavailable (e.g. the streaming path). Never raises --
    a failure here must not break an otherwise valid response.
    """
    try:
        from mcp_bridge.openai_clients import citation_guard

        if not citation_guard.get_citation_guard_enabled():
            return

        choices = getattr(response, "choices", None)
        if not choices:
            return
        message = getattr(choices[0], "message", None)
        if message is None:
            return
        content = getattr(message, "content", None)
        if not isinstance(content, str) or not content.strip():
            return

        evidence = getattr(request, "_citation_evidence", None) or {}
        evidence_urls = evidence.get("urls")
        evidence_available = evidence.get("available")
        if evidence_urls is None:
            # No attachment (unexpected): derive from the conversation itself.
            evidence_urls = citation_guard.collect_evidence_urls(
                getattr(request, "messages", None)
            )
            evidence_available = None

        new_content, report = citation_guard.annotate_response_content(
            content, evidence_urls, evidence_available=evidence_available
        )
        if not report.has_issues:
            return

        message.content = new_content
        try:
            trace_logger.record(
                "unverified_citations",
                placeholders=report.placeholders,
                unverified=report.unverified,
                evidence_size=len(evidence_urls or set()),
                evidence_available=report.evidence_available,
            )
        except Exception:
            pass
    except Exception:
        # The guard is best-effort; never let it break a valid response.
        pass



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
            # Citation provenance guard: flag any cited URL that cannot be
            # traced to the tool evidence actually retrieved this request, so a
            # fabricated citation is surfaced to the reader instead of being
            # presented as authoritative. Runs at the HTTP boundary so every
            # return path inside `chat_completions` is covered exactly once.
            _apply_citation_guard(request, response, trace_logger)

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
