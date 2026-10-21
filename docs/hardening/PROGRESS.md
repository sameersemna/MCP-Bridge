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

## Items
| ID | Title | Status (todo/in-progress/done/deferred/blocked) | Commit | Notes |
|----|-------|------|--------|-------|
| P0-01 | Secrets in repo | done | 5c29ad6 | 4 values replaced with `${ENV_VAR}`; scanner + pytest + pre-commit + CI job. History not rewritten (D1). **Rotate the exposed keys.** |
| P0-02 | Shared upstream pool closed per request | done | 6b54606 | Proxy transport with no-op `aclose`; `close_shared_transport()` called from lifespan. 4 new tests. |
| P0-03 | No graceful shutdown | todo | | |
| P0-04 | `[design]` Retries not idempotency-aware | todo | | |
| P0-05 | `[design]` Tool-result caches key on query only | todo | | |
| P0-06 | Aggregated SSE session map leaks | todo | | |
| P0-07 | Downstream SSE client never answers server requests | todo | | |
| P0-08 | Hand-rolled Streamable HTTP client | todo | | |
| P0-09 | Error resets kill healthy sessions | todo | | |
| P0-10 | REST 500-instead-of-404 + HTTP contract | todo | | |
| P0-11 | Insecure-by-default exposure | todo | | |
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
