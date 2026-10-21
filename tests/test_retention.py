"""Bounded on-disk growth and PII-safe trace payloads (P1-06)."""

from __future__ import annotations

import os
import time
from pathlib import Path

import mcp_bridge.logging as logging_module
import mcp_bridge.openai_clients.utils as openai_utils
from mcp_bridge.logging import (
    RequestTraceLogger,
    prune_trace_logs,
    redact_sensitive_data,
)
from mcp_bridge.retention import prune_tool_cache


def _touch(path: Path, *, age_seconds: float = 0, size: int = 16) -> None:
    path.write_bytes(b"x" * size)
    timestamp = time.time() - age_seconds
    os.utime(path, (timestamp, timestamp))


def test_prune_trace_logs_by_count(tmp_path) -> None:
    for index in range(5):
        _touch(tmp_path / f"{index}.json", age_seconds=(5 - index) * 10)

    removed = prune_trace_logs(
        tmp_path, max_files=2, max_age_days=0, max_total_mb=0
    )

    assert removed == 3
    assert len(list(tmp_path.glob("*.json"))) == 2


def test_prune_trace_logs_by_age(tmp_path) -> None:
    _touch(tmp_path / "old.json", age_seconds=10 * 86400)
    _touch(tmp_path / "new.json")

    removed = prune_trace_logs(
        tmp_path, max_files=0, max_age_days=1, max_total_mb=0
    )

    assert removed == 1
    assert (tmp_path / "new.json").exists()
    assert not (tmp_path / "old.json").exists()


def test_prune_trace_logs_by_total_size(tmp_path) -> None:
    _touch(tmp_path / "old.json", age_seconds=20, size=1024 * 1024)
    _touch(tmp_path / "new.json", age_seconds=10, size=1024 * 1024)

    removed = prune_trace_logs(
        tmp_path, max_files=0, max_age_days=0, max_total_mb=1
    )

    assert removed == 1
    assert (tmp_path / "new.json").exists()
    assert not (tmp_path / "old.json").exists()


def test_trace_logger_can_be_disabled(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(logging_module, "LOG_DIR", tmp_path)
    monkeypatch.setenv("MCP_BRIDGE_TRACE_LOG_ENABLED", "false")

    logger = RequestTraceLogger({"prompt": "x"}, "/v1/chat/completions", "POST")
    logger.record("test_event")

    assert list(tmp_path.glob("*.json")) == []


def test_redaction_scrubs_credentials_in_urls_and_headers() -> None:
    data = {
        "url": "https://example.test/a?api_key=super-secret&x=1",
        "headers": {"Authorization": "Bearer super-token"},
        "nested": ["token=another-secret"],
    }

    redacted = redact_sensitive_data(data)
    rendered = str(redacted)

    assert "super-secret" not in rendered
    assert "super-token" not in rendered
    assert "another-secret" not in rendered
    assert "x=1" in redacted["url"]
    assert redacted["headers"]["Authorization"] == "[REDACTED]"


def test_prune_tool_cache_by_entry_count(tmp_path) -> None:
    cache = openai_utils.PersistentToolCache(
        cache_dir=str(tmp_path), ttl_seconds=3600
    )
    result = {"isError": False, "content": [{"type": "text", "text": "ok"}]}
    for index in range(4):
        cache.put("tool", {"query": f"q{index}"}, result)
        time.sleep(0.01)

    removed = prune_tool_cache(cache, max_entries=1, max_mb=0)

    assert removed >= 1
    assert len(list(tmp_path.glob("*.json"))) <= 1
