"""Periodic retention for trace logs and the on-disk tool cache.

Trace files are written per request and the disk cache only expired entries
when they were read again, so both grew without bound. The lifespan starts one
background loop that prunes both.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

from loguru import logger

from mcp_bridge.logging import prune_trace_logs

DEFAULT_RETENTION_INTERVAL_SECONDS = 3600.0
DEFAULT_TOOL_CACHE_MAX_ENTRIES = 5000
DEFAULT_TOOL_CACHE_MAX_MB = 500.0


def _env_float(name: str, default: float, *, minimum: float = 0.0) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value >= minimum else default


def get_retention_interval_seconds() -> float:
    return _env_float(
        "MCP_BRIDGE_RETENTION_INTERVAL_SECONDS",
        DEFAULT_RETENTION_INTERVAL_SECONDS,
        minimum=1.0,
    )


def get_tool_cache_max_entries() -> int:
    return int(
        _env_float("MCP_BRIDGE_TOOL_CACHE_MAX_ENTRIES", DEFAULT_TOOL_CACHE_MAX_ENTRIES)
    )


def get_tool_cache_max_mb() -> float:
    return _env_float("MCP_BRIDGE_TOOL_CACHE_MAX_MB", DEFAULT_TOOL_CACHE_MAX_MB)


def prune_tool_cache(
    cache=None,  # type: ignore[no-untyped-def]
    *,
    max_entries: int | None = None,
    max_mb: float | None = None,
) -> int:
    """Prune the on-disk tool cache. Redis entries already expire via TTL."""
    from mcp_bridge.openai_clients import utils as openai_utils

    active = cache if cache is not None else openai_utils.get_tool_cache()
    if not isinstance(active, openai_utils.PersistentToolCache):
        return 0

    directory = Path(getattr(active, "_cache_dir", ""))
    if not directory.is_dir():
        return 0

    ttl = float(getattr(active, "_ttl_seconds", 0) or 0)
    files = sorted(
        (path for path in directory.glob("*.json") if path.is_file()),
        key=lambda path: path.stat().st_mtime,
    )
    removed = 0
    now = time.time()

    if ttl > 0:
        for path in list(files):
            try:
                age = now - path.stat().st_mtime
            except OSError:
                files.remove(path)
                continue
            if age > ttl:
                try:
                    path.unlink()
                    removed += 1
                except OSError:
                    pass
                files.remove(path)

    limit_entries = (
        get_tool_cache_max_entries() if max_entries is None else max_entries
    )
    limit_mb = get_tool_cache_max_mb() if max_mb is None else max_mb
    limit_bytes = int(limit_mb * 1024 * 1024) if limit_mb > 0 else 0
    total_bytes = 0
    for path in files:
        try:
            total_bytes += path.stat().st_size
        except OSError:
            pass

    while files and (
        (limit_entries > 0 and len(files) > limit_entries)
        or (limit_bytes > 0 and total_bytes > limit_bytes)
    ):
        path = files.pop(0)
        try:
            size = path.stat().st_size
            path.unlink()
            removed += 1
            total_bytes = max(0, total_bytes - size)
        except OSError:
            pass

    return removed


async def retention_loop(interval_seconds: float | None = None) -> None:
    """Prune trace logs and the disk cache periodically until cancelled."""
    interval = (
        interval_seconds
        if interval_seconds is not None
        else get_retention_interval_seconds()
    )
    while True:
        try:
            traces = prune_trace_logs()
            cached = prune_tool_cache()
            if traces or cached:
                logger.info(
                    f"retention: removed {traces} trace file(s) and "
                    f"{cached} cache entrie(s)"
                )
        except Exception as exc:
            # Retention is best-effort; it must never take down the bridge.
            logger.warning(f"retention sweep failed: {exc}")
        await asyncio.sleep(interval)


__all__ = [
    "get_retention_interval_seconds",
    "get_tool_cache_max_entries",
    "get_tool_cache_max_mb",
    "prune_tool_cache",
    "retention_loop",
]
