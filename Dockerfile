FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates git build-essential \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

WORKDIR /app
COPY pyproject.toml README.md uv.lock ./
COPY mcp_bridge/__init__.py mcp_bridge/__init__.py
RUN uv pip install --system "duckduckgo-mcp-server[browser]" neo4j-mcp-server \
    && uv sync --frozen --no-dev

COPY mcp_bridge ./mcp_bridge

RUN addgroup --system appgroup \
    && adduser --system --ingroup appgroup appuser \
    && mkdir -p /home/appuser/.cache/uv /app/logs /app/tool_cache \
    && chown -R appuser:appgroup /app /home/appuser
ENV HOME=/home/appuser \
    UV_CACHE_DIR=/home/appuser/.cache/uv
USER appuser

EXPOSE 11410
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:11410/health', timeout=2).read()" || exit 1

# Run as the unprivileged `appuser`. The `/app/logs` directory is created and
# owned by `appuser` during the build above, so no root privileges are needed
# at runtime (previously the ENTRYPOINT switched back to root to `mkdir` it).
ENTRYPOINT ["uv", "run", "--no-dev", "python", "-m", "mcp_bridge.main"]
