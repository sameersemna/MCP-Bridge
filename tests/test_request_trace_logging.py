import json
import re
from pathlib import Path

from mcp_bridge.logging import RequestTraceLogger


def test_request_trace_logger_writes_timestamped_json(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("mcp_bridge.logging.LOG_DIR", tmp_path)

    logger = RequestTraceLogger(
        request_payload={"messages": [{"role": "user", "content": "hello"}]},
        http_path="/v1/chat/completions",
        method="POST",
    )
    logger.record("incoming_request", payload={"prompt": "hello"})

    files = list(tmp_path.glob("*.json"))
    assert len(files) == 1

    content = files[0].read_text(encoding="utf-8")
    assert re.match(r"\d{8}T\d{6}Z_.*\.json", files[0].name)

    data = json.loads(content)
    assert data["request"]["messages"][0]["content"] == "hello"
    assert data["events"][0]["type"] == "incoming_request"
    assert data["events"][0]["payload"]["prompt"] == "hello"

    # A request with no error/degradation events is neither failed nor degraded.
    assert data["summary"]["failed"] is False
    assert data["summary"]["failure_reason"] is None
    assert data["summary"]["degraded"] is False
    assert data["summary"]["degradation_reason"] is None


def test_request_trace_logger_summary_reports_hard_failure(tmp_path: Path, monkeypatch) -> None:
    """A trace file must be self-describing: a hard failure (the request never
    got a response) shows up in the summary without reading every event."""
    monkeypatch.setattr("mcp_bridge.logging.LOG_DIR", tmp_path)

    logger = RequestTraceLogger(
        request_payload={"model": "test"},
        http_path="/v1/chat/completions",
        method="POST",
    )
    logger.record("incoming_request", prompt={"model": "test"})
    logger.record(
        "error",
        status_code=502,
        stage="upstream_error_status",
        model="test",
        detail="Upstream inference server returned status 400: invalid params (2013)",
    )

    data = json.loads(Path(logger.path).read_text(encoding="utf-8"))
    assert data["summary"]["failed"] is True
    assert "invalid params" in data["summary"]["failure_reason"]
    assert data["summary"]["degraded"] is False


def test_request_trace_logger_summary_reports_degraded_response(tmp_path: Path, monkeypatch) -> None:
    """A trace file for a request that returned HTTP 200 with a synthesized
    fallback (not the model's own answer) must be distinguishable from a
    genuine success at a glance."""
    monkeypatch.setattr("mcp_bridge.logging.LOG_DIR", tmp_path)

    logger = RequestTraceLogger(
        request_payload={"model": "test"},
        http_path="/v1/chat/completions",
        method="POST",
    )
    logger.record("incoming_request", prompt={"model": "test"})
    logger.record("degraded_response", reason="empty_response", trace_path=str(logger.path))

    data = json.loads(Path(logger.path).read_text(encoding="utf-8"))
    assert data["summary"]["failed"] is False
    assert data["summary"]["degraded"] is True
    assert data["summary"]["degradation_reason"] == "empty_response"
