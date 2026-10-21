from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

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
from mcp_bridge.idempotency import (
    get_idempotency_store,
    make_fingerprint,
    request_identity,
)
import json

router = APIRouter(prefix="/v1", tags=[Tag.openai])
tracer = trace.get_tracer("mcp_bridge.endpoints")


def _idempotency_error(status: int, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={
            "error": {
                "message": message,
                "type": "invalid_request_error",
                "code": "idempotency_key",
            }
        },
    )


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
        # Return an OpenAI-shaped error rather than raising NotImplementedError,
        # which surfaced as an opaque 500.
        return JSONResponse(
            status_code=501,
            content={
                "error": {
                    "message": (
                        "Streaming completions are not supported; use "
                        "/v1/chat/completions with stream=true."
                    ),
                    "type": "invalid_request_error",
                    "param": "stream",
                    "code": "unsupported",
                }
            },
        )
    else:
        return await completions(request, http_request)


@router.post("/chat/completions")
async def openai_chat_completions(
    request: CreateChatCompletionRequest, 
    http_request: Request
):
    """Chat Completions endpoint"""
    idempotency_key = http_request.headers.get("Idempotency-Key")
    if idempotency_key and request.stream:
        return _idempotency_error(
            400,
            "Idempotency-Key is not supported for streaming requests",
        )

    idempotency_store = get_idempotency_store() if idempotency_key else None
    if idempotency_store is not None and idempotency_key:
        fingerprint = make_fingerprint(
            identity=request_identity(http_request),
            method=http_request.method,
            path=http_request.url.path,
            body=request.model_dump(
                exclude_defaults=True, exclude_none=True, exclude_unset=True
            ),
        )
        state, entry = await idempotency_store.begin(idempotency_key, fingerprint)
        if state == "mismatch":
            return _idempotency_error(
                422,
                "Idempotency-Key was reused with a different request body",
            )
        if state == "in_progress":
            return _idempotency_error(
                409,
                "A request with this Idempotency-Key is still in progress",
            )
        if state == "replay":
            if entry is None or entry.body is None:
                return _idempotency_error(
                    500, "Stored idempotent response is no longer available"
                )
            headers = dict(entry.headers)
            headers["Idempotent-Replayed"] = "true"
            return JSONResponse(
                status_code=entry.status or 200,
                content=entry.body,
                headers=headers,
            )

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
        try:
            if request.stream:
                response = await streaming_chat_completions(request, http_request, trace_logger)
            else:
                response = await chat_completions(request, http_request, trace_logger)

            if response is None:
                # Defense in depth: no code path should produce a null response,
                # but guard against it so clients never see an HTTP 200 with a
                # null body.
                trace_logger.record("outgoing_response", response=None)
                raise HTTPException(status_code=502, detail="Chat completion produced no response")
        except Exception:
            if idempotency_store is not None and idempotency_key:
                await idempotency_store.release(idempotency_key)
            raise

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

        if idempotency_store is not None and idempotency_key and not request.stream:
            body = response.model_dump(
                exclude_defaults=True, exclude_none=True, exclude_unset=True
            )
            await idempotency_store.complete(
                idempotency_key,
                status=200,
                body=body,
                body_bytes=len(json.dumps(body, default=str).encode("utf-8")),
            )
        return response


@router.get("/models")
async def models(request: Request):
    """List models"""
    async with get_client(request) as client:
        response = await client.get("/models")
    if response.status_code >= 400:
        # Propagate the upstream status instead of returning an error body as
        # if it were a successful model list.
        try:
            body = response.json()
        except Exception:
            body = {
                "error": {
                    "message": f"Upstream model list failed with status {response.status_code}",
                    "type": "upstream_error",
                }
            }
        return JSONResponse(status_code=response.status_code, content=body)
    return response.json()
