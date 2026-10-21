# Client configuration samples

These files are starting points for MCP clients (Copilot, Kilo/Cline,
Continue, and others) that talk to MCP-Bridge or to third-party MCP servers.

**They contain no credentials.** Every secret value is an `${ENV_VAR}`
placeholder, for example:

```json
"headers": {
  "CONTEXT7_API_KEY": "${CONTEXT7_API_KEY}"
}
```

Export the variables in the environment that launches the client:

```bash
export CONTEXT7_API_KEY=...
export TAVILY_API_KEY=...
```

`${VAR}` is also the substitution syntax supported by MCP-Bridge's own config
loader, so the same form works in `config.json`.

## Never commit a real value

`scripts/check_secrets.py` scans every git-tracked file and fails on
credential-looking values. It runs in `pytest` and in CI. Install the hook
once per clone:

```bash
uvx pre-commit install
```

If a key was ever committed, replacing it here is not enough: it is still in
git history. **Rotate the key with the provider**, then keep the new value in
the environment only.
