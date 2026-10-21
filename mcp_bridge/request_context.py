"""Request correlation: accept or generate ``X-Request-ID``.

The id is attached to the ASGI request state, loguru's context, the trace log
and (via the endpoint) the OpenTelemetry span, and is echoed in the response.
"""

from __future__ import annotations

from uuid import uuid4

from loguru import logger
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

REQUEST_ID_HEADER = "X-Request-ID"


def sanitize_request_id(value: str) -> str:
    """Keep the id safe for a filename and logs."""
    cleaned = "".join(
        char for char in value if char.isalnum() or char in {"-", "_", "."}
    ).strip(".")
    return cleaned[:128]


class RequestIDMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):  # type: ignore[no-untyped-def]
        incoming = request.headers.get(REQUEST_ID_HEADER, "")
        request_id = sanitize_request_id(incoming) if incoming else uuid4().hex
        request.state.request_id = request_id
        with logger.contextualize(request_id=request_id):
            response = await call_next(request)
        response.headers[REQUEST_ID_HEADER] = request_id
        return response


def get_request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "") or ""


__all__ = [
    "REQUEST_ID_HEADER",
    "RequestIDMiddleware",
    "get_request_id",
    "sanitize_request_id",
]
