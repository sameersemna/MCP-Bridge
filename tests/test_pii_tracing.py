"""Span payload previews are off by default and redacted when enabled (P2-02)."""

from __future__ import annotations

from mcp_bridge.config import config
from mcp_bridge.config.final import Settings
from mcp_bridge.telemetry import payload_preview


def test_payload_preview_is_empty_by_default(monkeypatch) -> None:
    monkeypatch.setattr(
        config.telemetry, "capture_payloads", False, raising=False
    )

    assert payload_preview({"prompt": "secret user text"}) == ""


def test_payload_preview_redacts_when_enabled(monkeypatch) -> None:
    monkeypatch.setattr(
        config.telemetry, "capture_payloads", True, raising=False
    )

    preview = payload_preview(
        {
            "url": "https://example.test/a?api_key=super-secret",
            "headers": {"Authorization": "Bearer super-token"},
        }
    )

    assert preview
    assert "super-secret" not in preview
    assert "super-token" not in preview
    assert "[REDACTED]" in preview


def test_settings_default_capture_payloads_false() -> None:
    assert Settings().telemetry.capture_payloads is False