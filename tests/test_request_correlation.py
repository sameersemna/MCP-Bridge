"""Request correlation: X-Request-ID in responses, logs and traces (P2-01)."""

from __future__ import annotations

import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

import mcp_bridge.logging as logging_module
from mcp_bridge.logging import RequestTraceLogger
from mcp_bridge.request_context import RequestIDMiddleware, sanitize_request_id


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestIDMiddleware)

    @app.get("/ping")
    async def ping():  # type: ignore[no-untyped-def]
        return {"ok": True}

    return app


def test_generated_request_id_is_echoed() -> None:
    response = TestClient(_app()).get("/ping")

    request_id = response.headers.get("X-Request-ID")
    assert request_id
    assert len(request_id) == 32


def test_supplied_request_id_is_preserved() -> None:
    response = TestClient(_app()).get(
        "/ping", headers={"X-Request-ID": "abc-123"}
    )

    assert response.headers["X-Request-ID"] == "abc-123"


def test_request_id_is_safe_for_a_filename() -> None:
    assert "/" not in sanitize_request_id("../../etc/passwd")
    assert "\\" not in sanitize_request_id(r"..\\..\\windows")


def test_trace_logger_records_the_request_id(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(logging_module, "LOG_DIR", tmp_path)
    logger = RequestTraceLogger(
        {"prompt": "x"},
        "/v1/chat/completions",
        "POST",
        request_id="req-abc",
    )

    logger.record("test_event")
    logger._write()

    assert "req-abc" in logger._path.name
    payload = json.loads(logger._path.read_text(encoding="utf-8"))
    assert payload["request_id"] == "req-abc"
