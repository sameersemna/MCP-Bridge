FROM python:3.12-slim AS builder
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# Compilers are needed to build wheels, but must not ship in the runtime image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

WORKDIR /app
COPY pyproject.toml README.md uv.lock ./
COPY mcp_bridge/__init__.py mcp_bridge/__init__.py
RUN uv sync --frozen --no-dev \
    && uv pip install --python /app/.venv/bin/python \
        "duckduckgo-mcp-server[browser]" neo4j-mcp-server

FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/home/appuser

# Node/npm stay in the runtime because stdio MCP servers are commonly launched
# with `npx`. The compilers used to build wheels stay in the builder stage.
RUN apt-get update \
    && apt-get install -y --no-install-recommends nodejs npm ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY mcp_bridge ./mcp_bridge

RUN addgroup --system appgroup \
    && adduser --system --ingroup appgroup appuser \
    && mkdir -p /home/appuser/.npm /app/logs /app/tool_cache \
    && chown -R appuser:appgroup /app /home/appuser
USER appuser

EXPOSE 8000
# Read the port from the same setting the app uses; the old hard-coded 11410
# did not match the config default (8000) and checked the wrong endpoint.
HEALTHCHECK --interval=120s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import os, urllib.request; port=os.getenv('MCP_BRIDGE__NETWORK__PORT','8000'); urllib.request.urlopen(f'http://127.0.0.1:{port}/health/live', timeout=2).read()" || exit 1

# Run as the unprivileged `appuser`; no `uv` is needed at runtime.
ENTRYPOINT ["/app/.venv/bin/python", "-m", "mcp_bridge.main"]
