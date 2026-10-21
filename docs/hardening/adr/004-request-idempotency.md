# ADR 004 — Request-level idempotency

- **Status:** Accepted (2026-10-21)
- **Backlog item:** P1-03
- **Spec:** `draft-ietf-httpapi-idempotency-key-header`

## Context

`/v1/chat/completions` (non-streaming) and `/mcp/tools/{name}/call` had no
request-level idempotency. Clients (Open WebUI, SDK retry logic) resend on a
network blip; each resend re-runs the whole tool loop and any side-effecting
tools, and pays upstream tokens again.

## Decision

Honour an optional `Idempotency-Key` header:

- **Fingerprint** = `sha256(api_key_id, method, path, canonical_body)`. The API
  key is hashed, never stored. Canonical JSON sorts keys and normalizes
  whitespace.
- **Store**: in-memory LRU by default; `redis.asyncio` when `REDIS_URL` is set.
  Redis uses `SET ... NX EX` for the in-progress marker. If Redis is
  unreachable, the store falls back to the in-memory store.
- **States**:
  - new key → execute, then store status/body with a configurable TTL
    (default 24 h);
  - same key, same fingerprint, complete → replay the stored body with
    `Idempotent-Replayed: true`;
  - same key, in progress → `409 Conflict`;
  - same key, different fingerprint → `422 Unprocessable Entity`.
- **Streaming**: a streaming chat request carrying the header is rejected with
  an OpenAI-shaped `400` (`code: unsupported`). Buffering a stream to replay it
  would defeat streaming; silently ignoring the header would be unsafe.
- **Size cap**: a body larger than 1 MiB is not stored for replay; a later
  replay returns a clear `500` instead of a wrong body.
- The feature is entirely opt-in: without the header, behaviour is unchanged.

## Alternatives

- **Middleware that buffers every request.** Rejected for now: replaying a
  read body downstream requires wrapping ASGI `receive`, and it would have to
  special-case streaming. The two mutating endpoints can call the store
  directly with far less surface area.
- **Store only the fingerprint, re-execute on replay.** Rejected: replay must
  not re-invoke tools.
- **A single global Redis key.** Rejected: keys must be scoped per API key and
  per endpoint; the fingerprint already encodes that.
- **Silently ignore the header on streaming.** Rejected: a client would
  believe its retry is safe when it is not.

## Consequences

- A replay never re-executes tools or upstream calls.
- Two clients that reuse a key with different bodies get a `422`, not the
  wrong response.
- An in-progress marker is released if the request raises, so a failed request
  can be retried with the same key.
- Memory use is bounded by an LRU cap (default 1024 entries) plus the TTL.
  Redis entries expire on their own.
- Operators sharing a Redis instance should keep the existing `REDIS_PREFIX` so
  tool-cache and idempotency keys remain namespaced.
