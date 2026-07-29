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
