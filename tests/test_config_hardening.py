from pathlib import Path

import pytest
from pydantic import ValidationError

from mcp_bridge.config.file import load_config
from mcp_bridge.config.final import Settings
from mcp_bridge.logging import redact_sensitive_data


def test_load_config_rejects_path_traversal(tmp_path: Path) -> None:
    secret_config = tmp_path / "secret.json"
    secret_config.write_text('{"inference_server": {"base_url": "http://example.com/v1"}}', encoding="utf-8")

    with pytest.raises(ValueError, match="outside"):
        load_config(str(secret_config))


def test_redact_sensitive_data_masks_secrets() -> None:
    payload = {"api_key": "secret", "nested": {"token": "abc123"}, "message": "ok"}

    redacted = redact_sensitive_data(payload)

    assert redacted["api_key"] == "[REDACTED]"
    assert redacted["nested"]["token"] == "[REDACTED]"
    assert redacted["message"] == "ok"


def test_settings_reject_invalid_ports() -> None:
    with pytest.raises(ValidationError):
        Settings(network={"port": 70000})
