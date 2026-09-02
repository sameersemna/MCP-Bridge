import json
import os
import sys
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


from loguru import logger

SENSITIVE_KEYWORDS = ("key", "token", "secret", "password", "authorization")


def _default_log_dir() -> Path:
    repo_root = Path(__file__).resolve().parent.parent
    configured = os.getenv("MCP_BRIDGE_LOG_DIR")
    candidates: list[Path] = []

    if configured:
        candidates.append(Path(configured).expanduser().resolve())

    candidates.extend([
        Path("/app/logs"),
        repo_root / "logs",
        Path("/tmp/mcp-bridge-logs"),
    ])

    for candidate in candidates:
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            test_file = candidate / ".write_test"
            test_file.write_text("ok", encoding="utf-8")
            test_file.unlink(missing_ok=True)
            return candidate.resolve()
        except OSError:
            continue

    return Path("/tmp/mcp-bridge-logs").resolve()


LOG_DIR = _default_log_dir()


def redact_sensitive_data(value: Any) -> Any:
    """Recursively redact secrets from dictionaries and lists."""

    if isinstance(value, Mapping):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            if any(keyword in str(key).lower() for keyword in SENSITIVE_KEYWORDS):
                redacted[str(key)] = "[REDACTED]"
            else:
                redacted[str(key)] = redact_sensitive_data(item)
        return redacted

    if isinstance(value, list):
        return [redact_sensitive_data(item) for item in value]

    return value


def configure_logging(level: str = "INFO") -> None:
    """Configure loguru to emit structured JSON logs to stderr."""

    logger.remove()
    logger.add(
        sys.stderr,
        level=level,
        enqueue=True,
        serialize=True,
        backtrace=False,
        diagnose=False,
    )


def log_event(message: str, **fields: Any) -> None:
    """Emit a structured event log entry while redacting secrets."""

    logger.info(json.dumps(redact_sensitive_data({"message": message, **fields})))


class RequestTraceLogger:
    """Persist a structured trace of a single request lifecycle to a timestamped JSON file."""

    def __init__(self, request_payload: Any, http_path: str, method: str) -> None:
        self.request_payload = redact_sensitive_data(request_payload)
        self.http_path = http_path
        self.method = method
        self.events: list[dict[str, Any]] = []
        self._timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self._path = LOG_DIR / f"{self._timestamp}_{self._sanitize_path(http_path)}.json"
        self._directory = LOG_DIR
        self._directory.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _sanitize_path(path: str) -> str:
        sanitized = path.strip("/").replace("/", "__") or "root"
        return sanitized.replace(" ", "_")

    def record(self, event_type: str, **payload: Any) -> None:
        self.events.append(
            {
                "type": event_type,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                **{k: redact_sensitive_data(v) for k, v in payload.items()},
            }
        )
        self._write()

    def _write(self) -> None:
        # A trace file should be self-describing at a glance: whether this
        # request hard-failed (an "error" event -- the request never got a
        # response) or soft-degraded (a "degraded_response" event -- it got a
        # 200 but the content is a synthesized fallback, not the model's own
        # answer). Without these, telling a real answer from a degraded one
        # required opening the file and reading through every event by hand.
        error_events = [event for event in self.events if event["type"] == "error"]
        degraded_events = [event for event in self.events if event["type"] == "degraded_response"]
        recovered_events = [event for event in self.events if event["type"] == "early_stop_recovered"]

        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "method": self.method,
            "path": self.http_path,
            "request": self.request_payload,
            "events": self.events,
            "summary": {
                "event_count": len(self.events),
                "last_event_type": self.events[-1]["type"] if self.events else None,
                "tool_events": sum(
                    1
                    for event in self.events
                    if event["type"] in {
                        "mcp_tool_calls",
                        "mcp_tool_result",
                        "mcp_tool_dispatch_attempt",
                        "mcp_tool_dispatch_result",
                        "tool_message",
                    }
                ),
                "llm_responses": sum(1 for event in self.events if event["type"] == "llm_response"),
                "failed": bool(error_events),
                "failure_reason": error_events[0].get("detail") if error_events else None,
                "degraded": bool(degraded_events),
                "degradation_reason": degraded_events[0].get("reason") if degraded_events else None,
                "early_stop_recovered": bool(recovered_events),
            },
        }
        try:
            self._path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        except PermissionError:
            fallback_dir = Path("/tmp/mcp-bridge-logs")
            fallback_dir.mkdir(parents=True, exist_ok=True)
            self._directory = fallback_dir
            self._path = fallback_dir / self._path.name
            self._path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")

    @property
    def path(self) -> Path:
        return self._path
