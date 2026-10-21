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
- [ ] Same `Idempotency-Key` never re-executes tools — **P1-03 (pending)**.
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
| P0-07 | Downstream SSE client never answers server requests | done | 02a435b | Answers ping inline, roots/list `{"roots":[]}`, sampling in a tracked task, -32601 otherwise. Advertises sampling only with models; HTTP advertises nothing. 6 tests. |
| P0-08 | Hand-rolled Streamable HTTP client | done | 7326223 | Session id capture/echo, `MCP-Protocol-Version`, `headers` + bearer auth, one client/session, 404 re-init, DELETE on close, `__version__` everywhere; header/auth values redacted. 9 tests. |
| P0-09 | Error resets kill healthy sessions | done | b9fe9ee | Application `McpError` no longer resets; transport errors do; single timeout does not; 3 consecutive timeouts within 60s trip a breaker; success clears it. 6 tests, all 6 verified failing pre-fix via a scoped stash. |
| P0-10 | REST 500-instead-of-404 + HTTP contract | done | 2c06a93 | `get_client` → None; immutable body default; canonical-name dispatch (REST + aggregated); OpenAI-style 501 for streaming completions; `/v1/models` propagates upstream status. 8 tests. |
| P0-11 | Insecure-by-default exposure | done | 7dbef54 | New `security.auth.required_for_non_loopback` (default false); loud warning or hard startup failure for unauthenticated non-loopback binds. `/health/live` public, `/health/ready` follows auth; `/health` unchanged. README Security section. 8 tests. |
| P0-12 | Config HTTP load has no timeout/status check | todo | | |
| P1-01 | `[design]` Tool registry | todo | | |
| P1-02 | Tool-name collisions / OpenAI name constraints | todo | | |
| P1-03 | `[design]` `Idempotency-Key` support | todo | | |
| P1-04 | Blocking I/O on the event loop | todo | | |
| P1-05 | Sequential startup | todo | | |
| P1-06 | Unbounded on-disk growth | todo | | |
| P1-07 | `[design]` MCP SDK/protocol modernization | todo | | |
| P1-08 | Client disconnect and cancellation | todo | | |
| P1-09 | Streaming/non-streaming tool parity | todo | | |
| P2-01 | Request correlation | todo | | |
| P2-02 | PII-safe tracing | todo | | |
| P2-03 | Metrics | todo | | |
| P2-04 | Limits and backpressure | todo | | |
| P2-05 | CI lint/type blocking | todo | | |
| P2-06 | Docker and Helm | todo | | |
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
