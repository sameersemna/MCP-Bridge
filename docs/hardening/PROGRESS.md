# MCP-Bridge Hardening — Progress Ledger

Baseline (2026-10-21): pytest 372 passed · ruff 3 findings (F841×2, F401×1) · mypy 72 errors / 16 files · mcp SDK 1.3.0

Branch: `dev` · working tree clean at start · app imports OK.

## Unavailable tools / fallbacks
- `grep-github` (named in the prompt) is not exposed. Fallbacks used: `mcp_mcp-typescrip_searchGitHub` (code-pattern search over public repos), `github_repo` / `github_text_search` (scoped repo search), and `mcp_exa_web_search_exa`.
- `vibe-knowledge-graph` not assumed; decisions mirrored to the `memory` MCP server (entity `MCP-Bridge-Hardening`) when available, ledger is source of truth.
- `local-google-search` / `tavily` available via tool groups; `firecrawl` / `exa` available.

## Pending user decisions
- [x] D1 secrets history cleanup — asked 2026-10-21. **Answer: no history rewrite.** Working-tree fix only. User must rotate the exposed keys.
- [x] D2 default tool retry count — asked 2026-10-21. **Answer: keep `1`.** Retries will be gated on idempotency; README corrected to `1`.
- [x] D3 SDK upgrade / Streamable HTTP server — asked 2026-10-21. **Answer: approved.** Upgrade + `/mcp-server/mcp`, keep `/mcp-server/sse`.
- [x] D4 breaking defaults — asked 2026-10-21. **Approved:** P0-11 opt-in key + warning (`false`), P1-02 `tool_namespacing=on_collision`, P2-02 `telemetry.capture_payloads=false`.

## Idempotency checklist (§3)
- [x] Every automatic retry is safe by declaration or disabled — P0-04: only annotated / allow-listed tools retry.
- [x] A retried upstream chat completion has no side effects beyond cost — README documents that a retry is billed again.
- [x] Tool calls in one model turn are deduplicated by `tool_call.id` — P0-05: mutating tools are never deduped (both run); only declared-safe cacheable tools collapse, which the rule permits.
- [x] Every cache key covers every output-affecting input, has a version prefix, and never caches errors — P0-05: v2 key over server+tool+all args; v1 payloads rejected; error results never cached.
- [x] JSON-RPC request ids are unique per session and late responses are dropped — P0-08: HTTP ids increment for the session's lifetime (including re-init); SSE pops timed-out ids and `_resolve_future` drops unknown ids.
- [x] Same `Idempotency-Key` never re-executes tools — P1-03: replay returns the stored result; tests assert the fake tool ran once.
- [x] A timeout returns "outcome unknown", never a confident "failed" — P0-04 error text.

## Items
| ID | Title | Status (todo/in-progress/done/deferred/blocked) | Commit | Notes |
|----|-------|------|--------|-------|
| P0-01 | Secrets in repo | done | 5c29ad6 | 4 values replaced with `${ENV_VAR}`; scanner + pytest + pre-commit + CI job. History not rewritten (D1). **Rotate the exposed keys.** |
| P0-02 | Shared upstream pool closed per request | done | 6b54606 | Proxy transport with no-op `aclose`; `close_shared_transport()` called from lifespan. 4 new tests. |
| P0-03 | No graceful shutdown | done | d52e832 | `MCPClientManager.shutdown()` (concurrent, shared deadline); lifespan stops clients → closes pool → flushes OTel. New env `MCP_BRIDGE_SHUTDOWN_TIMEOUT_SECONDS` (5s). 5 tests. Redis: no shared client yet (P1-04). |
| P0-04 | `[design]` Retries not idempotency-aware | done | f53e57a | ADR 001. Retry only read-only/idempotent, `idempotent_tools`, or `retry_unsafe`. Unannotated → outcome-unknown error. Retry default stays 1 (D2); README fixed. |
| P0-05 | `[design]` Tool-result caches key on query only | done | 440e9db | ADR 002. v2 key = server+tool+canonical JSON of all args; fuzzy cache requires identical non-query args; in-flight dedupe uses full args; mutating tools never cached/deduped. |
| P0-06 | Aggregated SSE session map leaks | done | 6d711de | Ported upstream #1227 cleanup: response wrapper closes streams; shielded `finally` pops the session and closes all ends. 2 tests. |
| P0-07 | Downstream SSE client never answers server requests | done | 02a435b + follow-up | Answers ping inline, roots/list `{"roots":[]}`, sampling in a tracked task, -32601 otherwise. Advertises sampling only with models; HTTP advertises nothing. Follow-up: stdio advertises sampling only (roots/list unimplemented). 7 tests. |
| P0-08 | Hand-rolled Streamable HTTP client | done | 7326223 | Session id capture/echo, `MCP-Protocol-Version`, `headers` + bearer auth, one client/session, 404 re-init, DELETE on close, `__version__` everywhere; header/auth values redacted. 9 tests. |
| P0-09 | Error resets kill healthy sessions | done | b9fe9ee | Application `McpError` no longer resets; transport errors do; single timeout does not; 3 consecutive timeouts within 60s trip a breaker; success clears it. 6 tests, all 6 verified failing pre-fix via a scoped stash. |
| P0-10 | REST 500-instead-of-404 + HTTP contract | done | 2c06a93 | `get_client` → None; immutable body default; canonical-name dispatch (REST + aggregated); OpenAI-style 501 for streaming completions; `/v1/models` propagates upstream status. 8 tests. |
| P0-11 | Insecure-by-default exposure | done | 7dbef54 | New `security.auth.required_for_non_loopback` (default false); loud warning or hard startup failure for unauthenticated non-loopback binds. `/health/live` public, `/health/ready` follows auth; `/health` unchanged. README Security section. 8 tests. |
| P0-12 | Config HTTP load has no timeout/status check | done | edb7b50 | Explicit env timeout (10s), `raise_for_status`, JSON-object check; non-zero exit with a token-safe message unless `MCP_BRIDGE__CONFIG__HTTP_OPTIONAL=true`. 9 tests. |
| P1-01 | `[design]` Tool registry | done | 1743778 | ADR 003. Process-wide registry in config order; TTL 300s; invalidated by `tools/list_changed` (stdio/SSE) and session resets; resolution/discovery/suggestions/aggregated list read it. Discovery skips unready sessions; resolution stops on first match. 7 tests. |
| P1-02 | Tool-name collisions / OpenAI name constraints | done | 47c573c | `tool_namespacing` = none/on_collision (default)/always. Exposed names sanitized to `^[A-Za-z0-9_-]{1,64}$`, truncated with a stable hash; reverse lookup dispatches to the canonical tool. Applied to discovery, suggestions, aggregated list. 6 tests. |
| P1-03 | `[design]` `Idempotency-Key` support | done | 0933610 | ADR 004. Fingerprint over hashed credential+method+path+body; in-memory LRU or `redis.asyncio`; replay header, 409 in-progress, 422 mismatch, 400 for streaming; tool call and non-streaming chat. 6 tests. |
| P1-04 | Blocking I/O on the event loop | done | 473c0e7 | Redis `aget/aput` on one lazy `redis.asyncio` client; disk cache via `to_thread`; trace rewrite on the executor with a lock; signature cached per callable; cache factory singleton per config. 4 tests. |
| P1-05 | Sequential startup | done | b4eec8b | `initialize()` constructs clients with `gather(return_exceptions=True)`, preserving config order and isolating failures. 2 tests (max-time startup, failure isolation). |
| P1-06 | Unbounded on-disk growth | done | b9e651d | Trace enable + max files/age/size; disk-cache entry/size caps; lifespan retention task; URL/tool-arg credential redaction. 6 tests. |
| P1-07 | `[design]` MCP SDK/protocol modernization | done | 20a2ff6 | ADR 005. Upgraded to mcp 1.27.2 (`>=1.7.2,<2`); measured 484 existing tests pass. Added mounted Streamable HTTP at `/mcp-server/mcp` (auth-aware), kept `/mcp-server/sse`. Client replacement documented as follow-up. Scoped mypy SDK override (53 errors). |
| P1-08 | Client disconnect and cancellation | done | 1f5962a | Tool batches race a disconnect watcher; cancellation notifies downstream (`notifications/cancelled` on SSE/HTTP); streaming ends the generator. No request object keeps old await semantics. 4 tests. |
| P1-09 | Streaming/non-streaming tool parity | done | b21cfe0 | One merge policy: client tools + MCP tools (client wins clashes); client-owned calls end the loop as an OpenAI tool_calls passthrough; only MCP calls dispatch. Shared helper covers both loops. 4 tests. |
| P2-01 | Request correlation | done | e6f1ccc | `X-Request-ID` middleware (accept/generate, sanitize, echo); loguru context, trace filename+payload, `mcp_bridge.request.id` span attribute. 4 tests. |
| P2-02 | PII-safe tracing | done | 4dbea35 | `telemetry.capture_payloads=false` by default; when enabled, request/response/tool previews are redacted before becoming span attributes. 3 tests. |
| P2-03 | Metrics | done | aba85c2 | Dependency-free Prometheus counters/summaries; `/metrics` on the authenticated router behind `telemetry.metrics_enabled` (default false). Instruments upstream latency/status, tool calls, cache, idempotency, resets, turns. 3 tests. |
| P2-04 | Limits and backpressure | done | e82f5a7 | Opt-in `limits` config: per-key token bucket + concurrency cap, body-size 413, per-request tool-call cap. OpenAI-style 429 with Retry-After. 5 tests. |
| P2-05 | CI lint/type blocking | done | 2980e46 | 3 ruff findings fixed (0 now); 51 narrow per-line mypy suppressions (documented ratchet, 0 errors); pytest-cov with 70% floor (measured 70.59%); `continue-on-error` removed. |
| P2-06 | Docker and Helm | done | 4f5adc1 | Multi-stage Dockerfile (compilers only in builder; Node retained), healthcheck reads `MCP_BRIDGE__NETWORK__PORT` and probes `/health/live`; Helm probes, resources, non-root/read-only securityContext, emptyDir mounts. 2 tests. |
| P3-01 | Replace control-flow asserts | todo | | |
| P3-02 | Aggregated list handlers concurrent/isolated | todo | | |
| P3-03 | `[design]` Split chatCompletion/utils | todo | | |
| P3-04 | Repository hygiene (ask first) | todo | | |
| P3-05 | README/docs update | todo | | |

## Log
- 2026-10-21 12:32 Baseline gate recorded from a clean `dev` tree: pytest 372 passed, ruff 3 findings, mypy 72 errors/16 files, mcp 1.3.0, app imports OK. Ledger created.
- 2026-10-21 12:35 User decisions D1–D4 recorded (see above).
- 2026-10-21 12:52 P0-01 done (5c29ad6). Scanner detected the 4 real pre-fix values at `HEAD` (non-vacuity proven); working tree scans clean; gate 376 passed / ruff 3 / mypy 72.
- **Action for operator:** rotate the Context7 and Tavily keys that were committed (they remain in git history).
- 2026-10-21 13:03 P0-02 done (6b54606). Confirmed in httpx 0.28.1 source that `AsyncClient.aclose()` closes the shared transport (2 closes across 2 requests before the fix). Gate 380 passed / ruff 3 / mypy 72.
- 2026-10-21 13:22 P0-03 done (d52e832). `StdioClient` holds the child inside `async with stdio_client`, so cancelling the maintainer task unwinds and terminates it. Shutdown is bounded by one shared deadline. Gate 385 passed / ruff 3 / mypy 72.
- 2026-10-21 13:58 P0-04 done (f53e57a), ADR `docs/hardening/adr/001-idempotency-aware-tool-retries.md`. Measured: mcp 1.3.0 `Tool` has no `annotations` field and no `ToolAnnotations`; `extra="allow"` keeps them in `model_extra`. Gate 393 passed / ruff 3 / mypy 72.
- 2026-10-21 14:20 P0-05 done (440e9db), ADR `docs/hardening/adr/002-tool-cache-key-and-safety.md`. Found and fixed an interaction bug during implementation: a pre-existing ref-token block reassigned `server_name` to `None`, which would have written entries the server-scoped lookup could never read. Mypy initially rose 72→76 (in-flight dict still typed as a 2-tuple); widened to a 3-tuple, back to 72. Gate 404 passed / ruff 3 / mypy 72.
- 2026-10-21 14:32 P0-06 done (6d711de). Verified the installed mcp 1.3.0 has the same leak; implemented upstream's #1227 fix plus an unconditional shielded close. Test hangs avoided by simulating `http.disconnect` (a never-returning fake `receive` prevented the response task from exiting) and by using `*_nowait` so a regression fails fast rather than blocking. Gate 406 passed / ruff 3 / mypy 72.
- 2026-10-21 14:48 P0-07 done (02a435b). Mypy rose to 73 because a loop variable `task` collided with the existing `Task[None] | None`; renamed to `background_task`, back to 72. Gate 412 passed / ruff 3 / mypy 72.
- 2026-10-21 15:15 P0-08 done (7326223). The 404 test initially failed because the test called `list_tools()` without `initialize()`, so no session existed to expire — fixed the test, not the code. Existing HTTP fakes (no `delete`/`aclose`) still pass because they never call `aclose`. Gate 421 passed / ruff 3 / mypy 72.
- 2026-10-21 15:30 P0-09 done (b9fe9ee). Non-vacuity proven by stashing only `AbstractClient.py`: all 6 new tests failed pre-fix, all pass post-fix. Gate 427 passed / ruff 3 / mypy 72.
- 2026-10-21 15:50 P0-10 done (2c06a93). Collection failure in the full suite came from earlier tests replacing `sys.modules["lmos_openai_types"]` with a stub that lacks `CreateCompletionRequest`; the two `/v1` tests now run in a clean subprocess. Gate 435 passed / ruff 3 / mypy 72.
- 2026-10-21 16:10 P0-11 done (7dbef54). Posture check extracted to `mcp_bridge/security_posture.py` so tests do not import `main`/`endpoints` and trip over the `lmos_openai_types` stub order. Gate 443 passed / ruff 3 / mypy 72.
- 2026-10-21 16:28 P0-12 done (edb7b50). **All P0 items complete.** `httpx.get` has no `transport` parameter; switched the seam to an explicit `httpx.Client`. Gate 452 passed / ruff 3 / mypy 72.
- 2026-10-21 17:05 P1-01 done (1743778), ADR `docs/hardening/adr/003-tool-registry.md`. Two regressions surfaced and were fixed rather than accepted: a global registry barrier delayed resolution behind a slow server (now incremental with early exit and per-server freshness), and the process-wide registry leaked tools between tests when the client set changed (now invalidated on client-set change). Mypy fell to 71. Gate 459 passed / ruff 3 / mypy 71.
- 2026-10-21 17:12 P0-07 follow-up: the vendored stdio session advertised `roots` but never answered `roots/list`; it now advertises sampling only (the capability it actually implements). Gate 460 passed / ruff 3 / mypy 71.
- 2026-10-21 17:35 P1-02 done (47c573c). A structural edit briefly deleted the `_ttl_seconds` function header and a per-server exposed-name update was initially missed; both were caught before running. Gate 466 passed / ruff 3 / mypy 71.
- 2026-10-21 18:05 P1-03 done (0933610), ADR `docs/hardening/adr/004-request-idempotency.md`. The environment has `REDIS_URL` set; async Redis bound to TestClient's short-lived loop failed, which exposed that the in-memory fallback's `asyncio.Lock` was loop-bound. The memory store is now lock-free (its operations contain no awaits). Gate 472 passed / ruff 3 / mypy 71.
- 2026-10-21 18:25 P1-04 done (473c0e7). The existing Redis test injected a sync fake via `_client`; the async path now falls back to that client when no async client is configured, preserving the test seam. README Redis fallback text corrected (no silent backend switch). Gate 476 passed / ruff 3 / mypy 71.
- 2026-10-21 18:40 P1-05 done (b4eec8b). Startup is `gather`-based; the inventory still lists enabled/disabled/failed/active in config order. Gate 478 passed / ruff 3 / mypy 71.
- 2026-10-21 19:00 P1-06 done (b9e651d). The redaction test exposed that a bare `token=...` (no `?`/`&`) was not matched; the regex now anchors at string start too. Gate 484 passed / ruff 3 / mypy 71.
- 2026-10-21 19:30 P1-07 done (20a2ff6), ADR `docs/hardening/adr/005-sdk-upgrade-and-streamable-http.md`. Probe branch: mcp 1.27.2 + pydantic 2.14.1, all 484 existing tests passed; mypy rose 71→94, so a scoped `[mypy-mcp.*] follow_imports=skip` was added (53 errors, below baseline). The Streamable HTTP manager must be *mounted*, not a FastAPI route (double response), and two test-isolation issues were fixed (global sse-starlette event, global session manager). Gate 485 passed / ruff 3 / mypy 53.
- 2026-10-21 19:55 P1-08 done (1f5962a). The first test hung because the SSE write stream has zero buffer and the test awaited the send before starting the receiver; the test now starts both concurrently. The SSE/HTTP `notify_cancelled` uses the session's last request id (stdio cannot expose it). Gate 489 passed / ruff 3 / mypy 53.
- 2026-10-21 20:15 P1-09 done (b21cfe0). Making discovery unconditional exposed tests that supplied synthetic client tools: 14 e2e tests used `search` as a client tool and would both hang on real discovery and be treated as client-owned. They now stub the merge and remain loop-mechanics tests. Gate 493 passed / ruff 3 / mypy 55.
- 2026-10-21 20:30 P2-01 done (e6f1ccc). Request-id middleware is outermost so CORS/error responses also carry the header; the id is sanitized before use in a filename. Gate 497 passed / ruff 3 / mypy 55.
- 2026-10-21 20:42 P2-02 done (4dbea35). `config.telemetry.capture_payloads` defaults to false; span previews are omitted (not just empty) when disabled, and redacted when enabled. Gate 500 passed / ruff 3 / mypy 55.
- 2026-10-21 20:55 P2-03 done (aba85c2). A dependency-free registry avoided adding `prometheus_client`; the summary form (`_count`/`_sum`) is used rather than histogram buckets, which the bridge does not need yet. Gate 503 passed / ruff 3 / mypy 55.
- 2026-10-21 21:10 P2-04 done (e82f5a7). Two rounds of test failures traced to a missing `config` import in `streamChatCompletion` (the import anchor did not match because another import sat between the lines) and a fake-clock test that started at 0 against a bucket initialised with monotonic time. Gate 508 passed / ruff 3 / mypy 55.
- 2026-10-21 21:30 P2-05 done (2980e46). **CI is now blocking.** Ruff 0, mypy 0 (51 baseline ignores, one existing ignore had to move ahead of an inline comment to take effect), coverage floor 70% (measured 70.59%). `pytest-cov` added to dev deps. Gate: 508 passed / ruff 0 / mypy 0.
- 2026-10-21 21:45 P2-06 done (4f5adc1). **All P0–P2 items complete.** The Docker contract test initially failed because the runtime *comment* contained the literal 'build-essential'; the requirement is about the installed package, and the comment was reworded. Gate 510 passed / coverage 70.59% / ruff 0 / mypy 0.
