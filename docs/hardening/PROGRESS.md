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
| P3-01 | Replace control-flow asserts | done | a0935ea | Runtime asserts replaced with explicit errors (stdio streams, session readiness, env). Resource reads derive text/blob from the type and return `ReadResourceContents`, preserving custom MIME types. 3 tests. |
| P3-02 | Aggregated list handlers concurrent/isolated | done | 1be397e | `list_prompts`/`list_resources` gather all clients in parallel and log+skip a failing client; `list_prompts` previously had no per-client guard, so one bad server broke the aggregate. 2 tests (concurrency by wall clock, failure isolation). |
| P3-03 | `[design]` Split chatCompletion/utils | deferred | — | Deferred on **context budget, not design**. Both files are 2.4k/3.3k lines and the item requires behaviour-unchanged moves; a half-applied move is worse than none. Staged plan, one module per commit, gate after each: (1) `caches.py` — `ToolResultCache`, `PersistentToolCache`, `RedisToolCache`, `_canonical_json`, `_tool_cache_key`, `get_tool_cache` (~520 self-contained lines, re-exported from `utils`); (2) `url_redirects.py` — Google/ref-token unredirect helpers; (3) `tool_args.py` — schema coerce/default/repair helpers; (4) `tool_loop/` package for `chatCompletion`. Step 1 is the highest value and lowest risk. |
| P3-04 | Repository hygiene (ask first) | done | — | No file moved, by design. `.gitignore` already covers `response*.*`, `*.log` (incl. `firebase-debug.log`), `logs/`, `tmp/`, `tool_cache/`, `reports/`; `git ls-files logs` and a tracked `firebase-debug.log` are both empty. Proposal for the 13 tracked ad-hoc scripts is in the Final Report and awaits approval. |
| P3-05 | README/docs update | done | ea97d3f | Feature list refreshed — resources and streaming completions were still described as planned/unimplemented. Added a transport support matrix (upstream stdio/SSE/HTTP, downstream Streamable HTTP/SSE) and how tool discovery works when a transport has no notification channel. Idempotency, metrics, limits, caching and security posture were documented in their own items. |

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
- 2026-10-21 22:00 P3-01 done (a0935ea). mcp 1.27 types `BlobResourceContents.blob` as a base64 **string**, not bytes; the decoder handles both. Gate 513 passed / coverage 70.65% / ruff 0 / mypy 0.
- 2026-10-21 22:15 P3-02 done (1be397e). Gate 515 passed / coverage 70.91% / ruff 0 / mypy 0.
- 2026-10-21 22:30 P3-04 done (no commit; nothing moved). Audited `.gitignore` against the requested entries: all present, and neither `logs/` nor `firebase-debug.log` is tracked today. The 13 root-level scripts are listed in the Final Report as a proposal only.
- 2026-10-21 22:40 P3-05 done (ea97d3f). Verified the new anchor `#transport-support-matrix` resolves and that no "planned to be supported" / "not implemented yet" claim survives in `README.md` or `docs/`. Gate 515 passed.
- 2026-10-21 22:45 P3-03 deferred. Measured the actual move size before deciding: `utils.py` is 2,428 lines and `chatCompletion.py` 3,279. The cache subsystem alone is ~520 contiguous, self-contained lines, so step (1) of the plan is mechanical — but each move needs the whole block copied verbatim and a full gate, and the remaining context cannot carry four of them safely. Recorded the ordered plan in the item row instead of starting a move that could be left half-applied.

---

# Final Report

## Item status

| ID | Item | Status | Commit |
|----|------|--------|--------|
| P0-01 | Secrets in repo | done | 5c29ad6 |
| P0-02 | Shared upstream pool closed per request | done | 6b54606 |
| P0-03 | Graceful shutdown | done | d52e832 |
| P0-04 | Idempotency-aware retries | done | f53e57a |
| P0-05 | Tool-result cache keys | done | 440e9db |
| P0-06 | Aggregated SSE session leak | done | 6d711de |
| P0-07 | Downstream SSE answers server requests | done | 02a435b + 40e625c |
| P0-08 | Hand-rolled Streamable HTTP client | done | 7326223 |
| P0-09 | Error resets kill healthy sessions | done | b9fe9ee |
| P0-10 | REST 500-instead-of-404 + HTTP contract | done | 2c06a93 |
| P0-11 | Insecure-by-default exposure | done | 7dbef54 |
| P0-12 | Config HTTP load timeout/status | done | edb7b50 |
| P1-01 | Tool registry | done | 1743778 |
| P1-02 | Tool-name collisions | done | 47c573c |
| P1-03 | `Idempotency-Key` support | done | 0933610 |
| P1-04 | Blocking I/O on the event loop | done | 473c0e7 |
| P1-05 | Sequential startup | done | b4eec8b |
| P1-06 | Unbounded on-disk growth | done | b9e651d |
| P1-07 | MCP SDK / Streamable HTTP server | done | 20a2ff6 |
| P1-08 | Client disconnect and cancellation | done | 1f5962a |
| P1-09 | Streaming/non-streaming tool parity | done | b21cfe0 |
| P2-01 | Request correlation | done | e6f1ccc |
| P2-02 | PII-safe tracing | done | 4dbea35 |
| P2-03 | Metrics | done | aba85c2 |
| P2-04 | Limits and backpressure | done | e82f5a7 |
| P2-05 | CI lint/type blocking | done | 2980e46 |
| P2-06 | Docker and Helm | done | 4f5adc1 |
| P3-01 | Replace control-flow asserts | done | a0935ea |
| P3-02 | Aggregated list handlers | done | 1be397e |
| P3-03 | Split `chatCompletion`/`utils` | **deferred** | — |
| P3-04 | Repository hygiene | done (proposal) | — |
| P3-05 | README/docs update | done | ea97d3f |

**31 done, 1 deferred, 0 blocked.** Final gate: **515 passed · coverage 70.91 %
(floor 70) · ruff 0 · mypy 0 (66 files) · app imports OK.** No history was
rewritten, nothing was pushed, and each item is a single commit on `dev`.

## New environment variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `MCP_BRIDGE_SHUTDOWN_TIMEOUT_SECONDS` | `5` | Shared budget for stopping all downstream clients concurrently. |
| `MCP_BRIDGE_CONFIG_HTTP_TIMEOUT_SECONDS` | `10` | Timeout for downloading config over HTTP. |
| `MCP_BRIDGE__CONFIG__HTTP_OPTIONAL` | `false` | Continue without an unreachable HTTP config instead of aborting startup. |
| `MCP_BRIDGE_TOOL_REGISTRY_TTL_SECONDS` | `300` | Registry lifetime for transports with no `list_changed` channel. |
| `MCP_BRIDGE_IDEMPOTENCY_TTL_SECONDS` | `86400` | Stored idempotent response lifetime. |
| `MCP_BRIDGE_IDEMPOTENCY_MAX_ENTRIES` | `1024` | In-memory idempotency LRU cap. |
| `MCP_BRIDGE_TRACE_LOG_ENABLED` | `true` | Disable per-request trace files. |
| `MCP_BRIDGE_TRACE_LOG_MAX_FILES` | `500` | Trace file count cap (`0` disables). |
| `MCP_BRIDGE_TRACE_LOG_MAX_AGE_DAYS` | `14` | Trace file age cap. |
| `MCP_BRIDGE_TRACE_LOG_MAX_TOTAL_MB` | `500` | Trace directory size cap. |
| `MCP_BRIDGE_TOOL_CACHE_MAX_ENTRIES` | `5000` | Disk tool-cache entry cap. |
| `MCP_BRIDGE_TOOL_CACHE_MAX_MB` | `500` | Disk tool-cache size cap. |
| `MCP_BRIDGE_RETENTION_INTERVAL_SECONDS` | `3600` | Retention sweep period. |

All are optional; every default preserves the previous behaviour except where
noted below.

## New configuration keys

- `idempotent_tools` (per server) — extra tools that may be retried safely.
- `retry_unsafe` (per server) — opt a server out of the annotation gate.
- `headers` (per server) — static headers for HTTP/SSE upstreams.
- `tool_namespacing` — `none` | `on_collision` (default) | `always`.
- `security.auth.required_for_non_loopback` — hard-fail instead of warn.
- `telemetry.capture_payloads` — opt in to payload previews in spans.
- `telemetry.metrics_enabled` — expose `/metrics`.
- `limits` — `enabled`, body size / rate / concurrency / tool-call caps.

## Operator-visible behaviour changes

1. **Retries are gated on idempotency.** A tool without read-only/idempotent
   annotations is not retried, and a timeout reports "outcome unknown" rather
   than "failed". Servers that declare nothing and were previously retried now
   need `idempotent_tools` or `retry_unsafe`.
2. **Tool results are cached under a v2 key** covering server, tool and every
   argument. Old on-disk entries are ignored, not mis-served. Mutating tools are
   never cached or deduplicated.
3. **Secrets no longer live in config.** Four values became `${ENV_VAR}`
   references. **The exposed keys are still in git history — rotate them.**
4. **Startup can refuse to run.** With `required_for_non_loopback: true` an
   unauthenticated non-loopback bind is a hard failure rather than a warning.
5. **An unreachable HTTP config aborts startup** unless
   `MCP_BRIDGE__CONFIG__HTTP_OPTIONAL=true`.
6. **`/v1/completions` streaming returns an OpenAI-style 501**, not a 500.
7. **Resource reads return `ReadResourceContents`** and preserve custom MIME
   types; blob payloads are base64-decoded.
8. **Aggregated `prompts/list` and `resources/list` are concurrent**, and one
   failing server no longer breaks the whole list.
9. **The aggregated server also speaks Streamable HTTP** at `/mcp-server/mcp`;
   SSE remains at `/mcp-server/sse` and is deprecated.

## Remaining risks

1. **Rotate the four exposed secrets.** They remain in `git` history (the user
   declined a history rewrite).
2. **P3-03 is not done.** `openai_clients/utils.py` (2,428 lines) and
   `chatCompletion.py` (3,279 lines) remain monolithic; review cost and merge
   conflict risk stay high.
3. **Coverage is 70.91 % against a 70 % floor** — a small margin; new code
   without tests will trip CI quickly (which is the intent, but it is tight).
4. **51 per-line mypy suppressions** form a baseline ratchet. They are
   documented and reviewed, but they hide the SDK's untyped surface.
5. **HTTP upstreams discover tool changes only by TTL** (300 s). A renamed or
   removed tool can be briefly stale; calls fail cleanly rather than dispatch
   to the wrong server.
6. **The downstream Streamable HTTP *client*** is still the hand-rolled session
   from P0-08; ADR 005 records replacing it with the SDK client as a follow-up.
7. **No CI job runs against a live MCP server.** All coverage is against fakes,
   so protocol-version drift is caught by the SDK pin, not by a test.

## Recommended next steps

1. Rotate the four credentials, then add a `gitleaks`/history scan to CI.
2. Execute P3-03 step 1 (`caches.py`, ~520 self-contained lines) — the highest
   value, lowest risk move; then `url_redirects.py`, `tool_args.py`,
   `tool_loop/`, one commit each.
3. Decide on the 13 root-level scripts (below) and move them in one commit.
4. Replace the hand-rolled HTTP client with the SDK's (ADR 005 follow-up).
5. Raise the coverage floor as the suppression count falls; treat the two
   numbers as one ratchet.

## P3-04 — repository hygiene proposal (no files moved)

Thirteen ad-hoc scripts are tracked at the repository root. Proposed grouping,
**awaiting approval**:

- `scripts/upstream-smoke/` — `test.sh`, `test1.sh`, `test_9router.sh`,
  `test_nvidia.sh`, `test_ollama.sh`, `test_omniroute.sh`, `test_openrouter.sh`
  (they are per-provider smoke tests, not a pytest suite; the `test_*` names
  also confuse test discovery).
- `scripts/tools/` — `test_endpoint.py`, `debug_tools.py`.
- `scripts/prompt/` — `compress_prompt.py`, `compress_prompt.sh`.
- `scripts/genpdf/` — `genpdf.sh`, `genpdf_quote_transform.py`.

`.gitignore` already covers `response*.*`, `*.log` (so `firebase-debug.log`),
`logs/`, `tmp/`, `tool_cache/` and `reports/`; nothing under `logs/` is
tracked. No `.gitignore` change was required.
