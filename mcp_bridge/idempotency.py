"""Request-level idempotency via the ``Idempotency-Key`` header.

The store is a small state machine: a key is inserted as ``in_progress``, then
completed with the response. A replay returns the stored response; a concurrent
duplicate gets 409; a reused key with a different body gets 422.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

DEFAULT_IDEMPOTENCY_TTL_SECONDS = 24 * 60 * 60
DEFAULT_IDEMPOTENCY_MAX_ENTRIES = 1024
DEFAULT_IDEMPOTENCY_MAX_BODY_BYTES = 1024 * 1024


def _ttl_seconds() -> int:
    raw = os.getenv("MCP_BRIDGE_IDEMPOTENCY_TTL_SECONDS")
    if raw is None:
        return DEFAULT_IDEMPOTENCY_TTL_SECONDS
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_IDEMPOTENCY_TTL_SECONDS
    return value if value > 0 else DEFAULT_IDEMPOTENCY_TTL_SECONDS


def _max_entries() -> int:
    raw = os.getenv("MCP_BRIDGE_IDEMPOTENCY_MAX_ENTRIES")
    if raw is None:
        return DEFAULT_IDEMPOTENCY_MAX_ENTRIES
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_IDEMPOTENCY_MAX_ENTRIES
    return value if value > 0 else DEFAULT_IDEMPOTENCY_MAX_ENTRIES


def _canonical_json(value: Any) -> str:
    def normalize(item: Any) -> Any:
        if isinstance(item, str):
            return " ".join(item.split())
        if isinstance(item, dict):
            return {
                str(key): normalize(val)
                for key, val in sorted(item.items(), key=lambda pair: str(pair[0]))
            }
        if isinstance(item, (list, tuple)):
            return [normalize(element) for element in item]
        return item

    return json.dumps(normalize(value), sort_keys=True, separators=(",", ":"))


def request_identity(request: Any) -> str:
    """A stable, non-reversible identity for the caller's credential."""
    header = ""
    try:
        header = request.headers.get("authorization", "")
    except Exception:
        pass
    return hashlib.sha256(header.encode("utf-8")).hexdigest()


def make_fingerprint(
    *, identity: str, method: str, path: str, body: Any
) -> str:
    material = _canonical_json(
        {
            "identity": identity,
            "method": method.upper(),
            "path": path,
            "body": body,
        }
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


@dataclass
class IdempotencyEntry:
    fingerprint: str
    state: str  # "in_progress" | "complete"
    status: int = 0
    body: Any = None
    headers: dict[str, str] = field(default_factory=dict)
    expires_at: float = 0.0


class InMemoryIdempotencyStore:
    def __init__(
        self,
        ttl_seconds: int | None = None,
        max_entries: int | None = None,
    ) -> None:
        self._ttl_seconds = ttl_seconds if ttl_seconds is not None else _ttl_seconds()
        self._max_entries = (
            max_entries if max_entries is not None else _max_entries()
        )
        self._entries: "OrderedDict[str, IdempotencyEntry]" = OrderedDict()

    def _prune(self) -> None:
        now = time.monotonic()
        expired = [key for key, entry in self._entries.items() if entry.expires_at <= now]
        for key in expired:
            self._entries.pop(key, None)
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)

    async def begin(
        self, key: str, fingerprint: str
    ) -> tuple[str, IdempotencyEntry | None]:
        # No awaits in this method: the dict operations are atomic within the
        # event loop, and keeping it lock-free lets the memory fallback work
        # across short-lived loops (e.g. a test client's per-request portal).
        self._prune()
        existing = self._entries.get(key)
        if existing is None:
            self._entries[key] = IdempotencyEntry(
                fingerprint=fingerprint,
                state="in_progress",
                expires_at=time.monotonic() + self._ttl_seconds,
            )
            self._entries.move_to_end(key)
            return "new", None
        if existing.fingerprint != fingerprint:
            return "mismatch", existing
        if existing.state == "in_progress":
            return "in_progress", existing
        self._entries.move_to_end(key)
        return "replay", existing

    async def complete(
        self,
        key: str,
        *,
        status: int,
        body: Any,
        headers: dict[str, str] | None = None,
        body_bytes: int = 0,
    ) -> None:
        entry = self._entries.get(key)
        if entry is None:
            return
        entry.state = "complete"
        entry.status = status
        entry.headers = dict(headers or {})
        entry.expires_at = time.monotonic() + self._ttl_seconds
        if body_bytes and body_bytes > DEFAULT_IDEMPOTENCY_MAX_BODY_BYTES:
            # Do not retain an enormous body; replay will say so explicitly.
            entry.body = None
        else:
            entry.body = body
        self._entries.move_to_end(key)

    async def release(self, key: str) -> None:
        entry = self._entries.get(key)
        if entry is not None and entry.state == "in_progress":
            self._entries.pop(key, None)


class RedisIdempotencyStore:
    """Redis-backed store using ``redis.asyncio`` and ``SET NX EX``."""

    def __init__(self, client: Any, *, prefix: str = "mcp-bridge") -> None:
        self._client = client
        self._prefix = prefix.rstrip(":")
        self._fallback = InMemoryIdempotencyStore()

    def _key(self, key: str) -> str:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return f"{self._prefix}:idem:{digest}"

    async def begin(
        self, key: str, fingerprint: str
    ) -> tuple[str, IdempotencyEntry | None]:
        redis_key = self._key(key)
        entry = IdempotencyEntry(
            fingerprint=fingerprint, state="in_progress", expires_at=0.0
        )
        payload = json.dumps(
            {"fingerprint": fingerprint, "state": "in_progress"}
        )
        try:
            created = await self._client.set(
                redis_key,
                payload,
                nx=True,
                ex=_ttl_seconds(),
            )
            if created:
                return "new", None
            raw = await self._client.get(redis_key)
            if not raw:
                # Expired between SET NX and GET: treat as a new request.
                await self._client.set(
                    redis_key,
                    payload,
                    ex=_ttl_seconds(),
                )
                return "new", None
            stored = json.loads(raw)
            entry.fingerprint = str(stored.get("fingerprint", ""))
            entry.state = str(stored.get("state", "in_progress"))
            entry.status = int(stored.get("status", 0) or 0)
            entry.body = stored.get("body")
            entry.headers = dict(stored.get("headers") or {})
            if entry.fingerprint != fingerprint:
                return "mismatch", entry
            if entry.state == "in_progress":
                return "in_progress", entry
            return "replay", entry
        except Exception as exc:
            logger.warning(f"redis idempotency unavailable ({exc}); using memory")
            return await self._fallback.begin(key, fingerprint)

    async def complete(
        self,
        key: str,
        *,
        status: int,
        body: Any,
        headers: dict[str, str] | None = None,
        body_bytes: int = 0,
    ) -> None:
        redis_key = self._key(key)
        stored_body = (
            None
            if body_bytes and body_bytes > DEFAULT_IDEMPOTENCY_MAX_BODY_BYTES
            else body
        )
        payload = json.dumps(
            {
                "state": "complete",
                "status": status,
                "body": stored_body,
                "headers": dict(headers or {}),
            }
        )
        try:
            await self._client.set(
                redis_key, payload, ex=_ttl_seconds()
            )
        except Exception as exc:
            logger.warning(f"redis idempotency write failed ({exc}); using memory")
            await self._fallback.complete(
                key,
                status=status,
                body=body,
                headers=headers,
                body_bytes=body_bytes,
            )

    async def release(self, key: str) -> None:
        try:
            raw = await self._client.get(self._key(key))
            if raw:
                stored = json.loads(raw)
                if stored.get("state") == "in_progress":
                    await self._client.delete(self._key(key))
        except Exception:
            pass


_store: Any = None


def get_idempotency_store() -> Any:
    """Return the process-wide store (Redis when configured, else memory)."""
    global _store
    if _store is not None:
        return _store
    url = os.getenv("REDIS_URL", "").strip()
    if url:
        try:
            from redis.asyncio import Redis

            client = Redis.from_url(url, decode_responses=True)
            _store = RedisIdempotencyStore(
                client, prefix=os.getenv("REDIS_PREFIX", "mcp-bridge")
            )
            return _store
        except Exception as exc:
            logger.warning(f"redis idempotency init failed ({exc}); using memory")
    _store = InMemoryIdempotencyStore()
    return _store


__all__ = [
    "DEFAULT_IDEMPOTENCY_TTL_SECONDS",
    "IdempotencyEntry",
    "InMemoryIdempotencyStore",
    "RedisIdempotencyStore",
    "get_idempotency_store",
    "make_fingerprint",
    "request_identity",
]
