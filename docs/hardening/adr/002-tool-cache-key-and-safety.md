# ADR 002 — Tool-result cache keys and cache safety

- **Status:** Accepted (2026-10-21)
- **Backlog item:** P0-05

## Context

Two independent defects shared one cause — a cache identity that was too weak:

1. `_tool_cache_key(tool_name, query)` hashed only the tool name and the
   `query` argument. Two calls with the same query but different
   `max_results`, `page`, `site`, `language` or date range collided, in the
   per-request fuzzy cache and in the 48 h disk/Redis cache. The second caller
   received the first caller's results.
2. Caching was gated per **server** (`"cached": true`), so every tool on that
   server — including mutating tools — could be cached or deduplicated by
   name+query.

## Decision

### Key

The exact-match key becomes:

```
sha256("v2" \x1f server \x1f tool \x1f canonical_json(all_arguments))
```

Canonical JSON sorts object keys, uses compact separators, and normalizes
whitespace inside string values, recursively. The server name is part of the
key because two servers may expose the same tool name.

The version marker is part of the hashed material, so a v1 filename can never
be addressed. The payload additionally carries `"key_version": 2`; a payload
with a different value is treated as a miss, so a copied or stale file is
inert. This satisfies "old v1 keys are not read" without a migration.

### Fuzzy cache

`ToolResultCache` still treats near-duplicate *queries* as hits, but only among
entries whose **non-query arguments** have identical canonical JSON. A
near-duplicate query with a different `site` or `max_results` is now a miss.

### In-flight dedupe

The in-flight key becomes `(server, tool, canonical_json(all_arguments))`
instead of `(tool, query)`.

### Mutating tools

`GenericMcpClient.is_cache_safe(tool)` reads the annotations recorded by
P0-04:

- `destructiveHint is true` → never cache or dedupe;
- `readOnlyHint is false` → never cache or dedupe;
- `readOnlyHint is true` → cache/dedupe as before;
- unannotated → keep the previous behaviour (the operator already opted in
  with `"cached": true`).

`call_tools` consults this only when it has a tool→server map. Legacy direct
callers (and tests) that pass no map keep the previous `cache_enabled`-only
behaviour, so the public surface stays compatible.

## Alternatives

- **Hash only the JSON payload string as received.** Rejected: key order and
  incidental whitespace would produce different keys for equivalent calls.
- **Keep the query-only key and add the other args to the value.** Rejected:
  it still returns the wrong cached result on a collision.
- **Gate caching per tool in config only.** Rejected: annotations are the
  server's own declaration and should be honoured; config gating would still
  require operators to enumerate every mutating tool.
- **Disable caching for unannotated tools.** Considered but rejected for
  backward compatibility: it would silently turn caching off for every server
  that predates annotations. The operator already opted in per server; the
  residual risk is documented instead.

## Consequences

- Differing non-query arguments now miss; identical arguments and equivalent
  JSON (key order/whitespace) hit.
- Old v1 entries are ignored, not mis-served. They expire naturally.
- A mutating tool on a `cached: true` server is no longer cached when it
  declares `readOnlyHint: false` or `destructiveHint: true`.
- The cache API accepts either a dict of arguments (new) or a string query
  (legacy), so existing callers keep working.
- Whitespace normalization means two string arguments that differ only in
  whitespace share a key. This is the item's explicit rule; it is safe for the
  read-only tools the cache is meant for.
