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
RUN uv sync --frozen --no-dev

COPY mcp_bridge ./mcp_bridge

RUN addgroup --system appgroup \
    && adduser --system --ingroup appgroup appuser \
    && mkdir -p /home/appuser/.cache/uv \
    && chown -R appuser:appgroup /app /home/appuser
ENV HOME=/home/appuser \
    UV_CACHE_DIR=/home/appuser/.cache/uv
USER appuser

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).read()" || exit 1

RUN uv pip install mcp-server-fetch

ENTRYPOINT ["uv", "run", "--no-dev", "python", "-m", "mcp_bridge.main"]
