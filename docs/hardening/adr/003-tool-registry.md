# ADR 003 — Tool registry with TTL and change notifications

- **Status:** Accepted (2026-10-21)
- **Backlog item:** P1-01

## Context

`McpClientManager.resolve_tool` probed **every** client with `tools/list` on
every call; the alias and server-name fallbacks listed every server again, and
`suggest_tools` did the same. `chat_completion_add_tools` re-discovered every
server on every request. With N servers and K tool calls per turn that is
O(N·K) extra RPCs. `asyncio.FIRST_COMPLETED` also made the winner
non-deterministic when two servers exposed the same tool name.

## Decision

Add `ToolRegistry`, owned by `MCPClientManager`:

- one entry per normalized tool name: `(server, canonical_name, tool, annotations)`;
- built from a single discovery pass and stored in **config order**, so a
  collision always resolves to the first configured server and is logged once;
- invalidated by a `notifications/tools/list_changed` notification from a
  stdio or SSE session, and on a session reset (a dead session may come back
  with different tools);
- invalidated on a TTL, `MCP_BRIDGE_TOOL_REGISTRY_TTL_SECONDS` (default 300);
- read by `resolve_tool`, `suggest_tools`, `chat_completion_add_tools` and the
  aggregated server's `list_tools`.

The request/response HTTP transport has no persistent notification stream, so
it relies on the TTL. That is stated in the code and README rather than
pretending the notification is handled.

## Alternatives

- **Cache `list_tools` per client with a per-client timestamp.** Simpler, but
  it does not give a single deterministic collision policy or one place for
  aliases and suggestions to read.
- **Refresh only on TTL.** Rejected: a server that adds/removes tools would be
  stale for up to five minutes even though it told us.
- **Refresh on every turn.** Rejected: that is the O(N·K) behaviour being
  removed.
- **Use `FIRST_COMPLETED` and accept a random winner.** Rejected: two servers
  exposing `search` would route non-deterministically between requests.

## Consequences

- After the first discovery, `resolve_tool` and `suggest_tools` make **zero**
  `tools/list` RPCs. Discovery and the aggregated `list_tools` make one pass
  over the registry instead of one RPC per server per request.
- A collision has a stable, documented winner (earlier config entry).
- `notifications/tools/list_changed` is handled for stdio and SSE; HTTP relies
  on the TTL. Tool annotations recorded during discovery continue to feed the
  retry and cache safety rules (P0-04, P0-05).
- The registry is process-wide state. It is rebuilt from live sessions, never
  persisted, so a restart starts clean.
