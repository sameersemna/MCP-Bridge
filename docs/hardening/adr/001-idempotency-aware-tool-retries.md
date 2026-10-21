# ADR 001 — Idempotency-aware tool-call retries

- **Status:** Accepted (2026-10-21)
- **Backlog item:** P0-04
- **User decision:** D2 — keep `MCP_BRIDGE_TOOL_RETRY_COUNT` default at `1`.

## Context

`GenericMcpClient.call_tool` re-executed the identical `tools/call` after a
timeout (`AbstractClient.py`, timeout branch). The default retry count was `1`,
while `README.md` documented `0`, and `compose.yml` set `1`. A timeout does not
tell the client whether the server executed the call, so the retry could
duplicate a file write, a sent message, a created ticket, or a memory entry.

MCP tool annotations (`readOnlyHint`, `idempotentHint`, `destructiveHint`,
spec 2025-03-26 and later) describe exactly this property, and the bridge
ignored them (`grep` found no reference).

### API constraint measured against the pin

The installed SDK is `mcp==1.3.0`. Measured with `uv run python`:

- `mcp.types.Tool.model_fields` = `name`, `description`, `inputSchema`.
- `mcp.types` has **no** `ToolAnnotations`; its `Annotations` model is an
  unrelated `audience`/`priority` type.
- `Tool` is declared with `model_config = ConfigDict(extra="allow")`, so a
  server's `annotations` object survives in `model_extra["annotations"]`.

Therefore the implementation reads the raw mapping from `model_extra`; it does
not import a typed model that does not exist in the pinned version.

## Decision

Retry a timed-out tool call **only when it is declared safe**:

1. `annotations.readOnlyHint is true`, or
2. `annotations.idempotentHint is true`, unless `destructiveHint is true` while
   `idempotentHint` is not true (a contradictory server is not trusted), or
3. the operator lists the canonical tool name in the server's
   `idempotent_tools: [..]`, or
4. the operator sets `retry_unsafe: true` for that server.

Unannotated tools are **not** retried. On a timeout they receive an `isError`
result that says the outcome is unknown and that the call was not retried, so
the model can verify instead of blindly repeating the call.

Annotations are recorded during discovery: the manager already lists a
client's tools before every dispatch (`resolve_tool`), and it now calls
`remember_tool_annotations` on the client. The rule therefore holds for
`/v1/chat/completions`, `/mcp/tools/{name}/call`, and the aggregated server.

## Alternatives

- **Change the default to `0`.** Rejected by D2; it also removes retries for
  genuinely read-only tools, which are the safe and useful case.
- **Retry all, deduplicate at the MCP layer.** Rejected: the server owns the
  side effect; the bridge cannot undo a write it cannot see.
- **Annotations only, no operator override.** Rejected: many real servers ship
  no annotations, and an operator sometimes knows a tool is idempotent.
- **Read a typed `ToolAnnotations`.** Not available in the pinned SDK; reading
  `model_extra` is the forward-compatible choice and will keep working after
  the SDK upgrade in P1-07.

## Consequences

- **Behavior change:** unannotated tools no longer retry after a timeout. This
  is the point of the item and is documented in `README.md`.
- `README.md` now documents the real default (`1`) and that retries are limited
  to declared-safe tools.
- New optional per-server keys `idempotent_tools` and `retry_unsafe`; both are
  stripped before transport validation, following the `cached` pattern, so
  `SSEMCPServer` (`extra="forbid"`) still loads.
- Session reset on timeout is unchanged here; P0-09 revisits when a reset is
  appropriate and how it interacts with in-flight calls.
- The timeout result is explicitly an unknown outcome, satisfying the
  idempotency checklist in §3 of the hardening prompt.
