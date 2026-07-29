import importlib
import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def test_request_trace_logger_writes_to_current_working_directory_logs(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MCP_BRIDGE_LOG_DIR", str(tmp_path / "logs"))

    import mcp_bridge.logging as logging_module

    logging_module = importlib.reload(logging_module)

    trace_logger = logging_module.RequestTraceLogger(
        request_payload={"messages": [{"role": "user", "content": "hello"}]},
        http_path="/v1/chat/completions",
        method="POST",
    )
    trace_logger.record("incoming_request", prompt="hello")

    assert (tmp_path / "logs").exists()
    assert trace_logger.path.parent == tmp_path / "logs"
    assert "incoming_request" in trace_logger.path.read_text(encoding="utf-8")


def test_request_trace_logger_counts_tool_dispatch_events(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MCP_BRIDGE_LOG_DIR", str(tmp_path / "logs"))

    import mcp_bridge.logging as logging_module

    logging_module = importlib.reload(logging_module)

    trace_logger = logging_module.RequestTraceLogger(
        request_payload={"messages": [{"role": "user", "content": "hello"}]},
        http_path="/v1/chat/completions",
        method="POST",
    )
    trace_logger.record("mcp_tool_dispatch_attempt", tool_name="search")
    trace_logger.record("mcp_tool_dispatch_result", tool_name="search", is_error=False)

    payload = json.loads(trace_logger.path.read_text(encoding="utf-8"))
    assert payload["summary"]["tool_events"] == 2
