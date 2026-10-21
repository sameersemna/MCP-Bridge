# ADR 005 — MCP SDK upgrade and Streamable HTTP server transport

- **Status:** Accepted (2026-10-21)
- **Backlog item:** P1-07
- **User decision:** D3 — upgrade approved, add `/mcp-server/mcp`.

## Context

The bridge pinned `mcp>=1.2.0,<=1.7.1` and installed `mcp==1.3.0`. It
vendors/hand-rolls the SSE server transport, the SSE client session and the
Streamable HTTP client session. Only the deprecated HTTP+SSE server transport
was exposed (`/mcp-server/sse`), which current clients (VS Code Copilot, Claude,
Cursor) no longer prefer.

## Measurement

On a throwaway branch (`sdk-upgrade-probe`, never pushed), `uv add "mcp>=1.7.2"`
resolved **mcp 1.27.2** and pydantic 2.14.1.

- The full existing suite (484 tests at the time) passed unchanged on 1.27.2.
- Adding the Streamable HTTP endpoint and its test brought it to **485 passed**.
- `mypy mcp_bridge` rose from 71 to 94 errors, concentrated in SDK-facing
  modules whose types changed. A scoped override
  (`[mypy-mcp.*] follow_imports = skip`) treats the third-party SDK as untyped
  and brings the count to **53**, below the baseline, while `mcp_bridge` code
  remains checked.
- `ruff` stayed at its 3 pre-existing findings.

## Decision

1. **Upgrade** to `mcp>=1.7.2,<2` (the `<2` bound prevents a future major
   version from breaking the build silently).
2. **Add** a Streamable HTTP endpoint at `/mcp-server/mcp`. The SDK's
   `StreamableHTTPSessionManager` is an ASGI application that writes its own
   response, so it is **mounted** rather than registered as a FastAPI route (a
   route caused a second `http.response.start`). A thin ASGI wrapper applies the
   same API-key check as `secure_router` when `security.auth.enabled` is true.
3. **Keep** `/mcp-server/sse` for backward compatibility.
4. **Keep the hand-rolled downstream client sessions for now.** They already
   pass on 1.27.2 and carry resilience that the SDK's `ClientSession` does not
   provide out of the box: ping suppression while a tool call is in flight, a
   readable `_describe_exception`, reconnect backoff, the P0-08 session
   id/protocol/DELETE behaviour, and the timeout circuit breaker. Replacing them
   is a separate refactor with its own test rewrite; doing it in the same change
   as the server transport would make a regression much harder to localise.
5. **Scope mypy** to skip following the `mcp` package, documented in
   `mypy.ini`. This is a third-party typing workaround, not a suppression in
   `mcp_bridge` code.

## Alternatives

- **Stay on 1.3.0 and hand-roll Streamable HTTP.** Rejected: the server
  transport is substantial (session table, event store, resumability), and the
  SDK now ships it.
- **Upgrade every dependency.** Rejected: only `mcp` and the pydantic it
  requires changed; pinning the rest keeps the change reviewable.
- **Replace the clients in the same commit.** Rejected as above; the measured
  upgrade already proves runtime compatibility, so the client refactor can be
  scheduled independently without blocking current clients.
- **Accept 94 mypy errors.** Rejected: the hardening gate forbids raising the
  count.

## Consequences

- Clients can connect over **both** `/mcp-server/mcp` (Streamable HTTP) and
  `/mcp-server/sse` (deprecated) and list the aggregated tools.
- The Streamable HTTP endpoint follows the auth setting; when auth is disabled
  it is public, exactly like the SSE bridge.
- `pyproject.toml`, `uv.lock`, `mypy.ini`, the lifespan (session-manager run
  context), and the tests change together. The session manager is process-wide
  in production; tests use a local manager to avoid coupling lifecycles.
- `test` isolation needed two fixes: reset sse-starlette's loop-bound
  `AppStatus.should_exit_event`, and stub the Streamable HTTP lifespan in tests
  that drive the app lifespan without intending to start the MCP server.
- The downstream client replacement remains a known, documented follow-up.
