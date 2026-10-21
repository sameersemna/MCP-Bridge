"""Opt-in rate limiting, per-key concurrency and request-size limits.

Defaults are off so existing deployments are unchanged. When enabled, errors
use the OpenAI error envelope and 429 responses carry ``Retry-After``.
"""

from __future__ import annotations

import time
from typing import Any

from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from mcp_bridge.config import config


class TokenBucket:
    def __init__(self, rate_per_minute: int, capacity: float | None = None) -> None:
        self.rate_per_second = max(1, rate_per_minute) / 60.0
        self.capacity = float(capacity if capacity is not None else max(1, rate_per_minute))
        self.tokens = self.capacity
        self.updated = time.monotonic()

    def allow(self, now: float | None = None) -> bool:
        current = time.monotonic() if now is None else now
        self.tokens = min(
            self.capacity,
            self.tokens + (current - self.updated) * self.rate_per_second,
        )
        self.updated = current
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True
        return False


class InMemoryLimiter:
    def __init__(self) -> None:
        self._buckets: dict[str, TokenBucket] = {}
        self._active: dict[str, int] = {}

    @staticmethod
    def key_for(request: Request) -> str:
        authorization = request.headers.get("authorization", "")
        if authorization:
            return authorization
        client = getattr(request, "client", None)
        return client.host if client else "anonymous"

    def try_acquire(self, request: Request) -> str | None:
        limits = config.limits
        key = self.key_for(request)
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = TokenBucket(limits.rate_limit_per_minute)
            self._buckets[key] = bucket
        if not bucket.allow():
            return "rate"
        active = self._active.get(key, 0)
        if active >= limits.max_concurrent_per_key:
            return "concurrency"
        self._active[key] = active + 1
        return None

    def release(self, request: Request) -> None:
        key = self.key_for(request)
        self._active[key] = max(0, self._active.get(key, 1) - 1)

    def reset(self) -> None:
        self._buckets.clear()
        self._active.clear()


limiter = InMemoryLimiter()


def _error(
    status: int, message: str, code: str, retry_after: str | None = None
) -> JSONResponse:
    headers = {"Retry-After": retry_after} if retry_after else None
    return JSONResponse(
        status_code=status,
        content={
            "error": {
                "message": message,
                "type": "rate_limit_error" if status == 429 else "invalid_request_error",
                "code": code,
            }
        },
        headers=headers,
    )


class LimitsMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Any) -> Any:
        limits = getattr(config, "limits", None)
        if limits is None or not limits.enabled:
            return await call_next(request)

        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > limits.max_body_bytes:
                    return _error(
                        413, "Request body is too large", "request_too_large"
                    )
            except ValueError:
                return _error(400, "Invalid Content-Length", "invalid_content_length")

        decision = limiter.try_acquire(request)
        if decision == "rate":
            return _error(
                429,
                "Rate limit exceeded; retry later",
                "rate_limited",
                retry_after="60",
            )
        if decision == "concurrency":
            return _error(
                429,
                "Too many concurrent requests for this key",
                "concurrency_limited",
                retry_after="1",
            )
        try:
            return await call_next(request)
        finally:
            limiter.release(request)


__all__ = ["InMemoryLimiter", "LimitsMiddleware", "TokenBucket", "limiter"]
