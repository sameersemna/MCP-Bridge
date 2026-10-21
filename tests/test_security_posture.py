"""Security posture and health-endpoint contract (P0-11).

Defaults stay backward compatible, but an unauthenticated non-loopback bind is
warned about (or refused), and readiness detail follows the auth setting.
"""

from __future__ import annotations

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from mcp_bridge import security_posture
from mcp_bridge.auth import get_api_key
from mcp_bridge.config import config
from mcp_bridge.config.final import ApiKey
from mcp_bridge.health.manager import manager as health_manager
from mcp_bridge.health.router import ready_router, router as health_router


def _public_app() -> FastAPI:
    app = FastAPI()
    app.include_router(health_router)
    return app


def _ready_app() -> FastAPI:
    app = FastAPI()
    # Mirrors mcp_bridge.routers: get_api_key is a no-op when auth is disabled.
    app.include_router(ready_router, dependencies=[Depends(get_api_key)])
    return app


def test_non_loopback_without_auth_logs_a_loud_warning(monkeypatch) -> None:
    monkeypatch.setattr(config.network, "host", "0.0.0.0", raising=False)
    monkeypatch.setattr(config.security.auth, "enabled", False, raising=False)
    monkeypatch.setattr(
        config.security.auth, "required_for_non_loopback", False, raising=False
    )
    warnings: list[str] = []
    monkeypatch.setattr(
        security_posture.logger, "warning", lambda message: warnings.append(message)
    )

    security_posture.enforce_security_posture()

    assert any("SECURITY WARNING" in message for message in warnings)


def test_loopback_without_auth_does_not_warn(monkeypatch) -> None:
    monkeypatch.setattr(config.network, "host", "127.0.0.1", raising=False)
    monkeypatch.setattr(config.security.auth, "enabled", False, raising=False)
    warnings: list[str] = []
    monkeypatch.setattr(
        security_posture.logger, "warning", lambda message: warnings.append(message)
    )

    security_posture.enforce_security_posture()

    assert warnings == []


def test_required_for_non_loopback_refuses_startup(monkeypatch) -> None:
    monkeypatch.setattr(config.network, "host", "0.0.0.0", raising=False)
    monkeypatch.setattr(config.security.auth, "enabled", False, raising=False)
    monkeypatch.setattr(
        config.security.auth, "required_for_non_loopback", True, raising=False
    )

    with pytest.raises(RuntimeError, match="SECURITY WARNING"):
        security_posture.enforce_security_posture()


def test_is_loopback_host_distinguishes_wildcards() -> None:
    assert security_posture.is_loopback_host("127.0.0.1")
    assert security_posture.is_loopback_host("127.0.0.2")
    assert security_posture.is_loopback_host("::1")
    assert security_posture.is_loopback_host("localhost")

    assert not security_posture.is_loopback_host("0.0.0.0")
    assert not security_posture.is_loopback_host("::")
    assert not security_posture.is_loopback_host("")
    assert not security_posture.is_loopback_host("192.168.1.5")


def test_health_live_is_minimal_and_public() -> None:
    response = TestClient(_public_app()).get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_ready_requires_auth_when_enabled(monkeypatch) -> None:
    monkeypatch.setattr(config.security.auth, "enabled", True, raising=False)
    monkeypatch.setattr(
        config.security.auth, "api_keys", [ApiKey(key="secret")], raising=False
    )
    monkeypatch.setattr(health_manager, "is_healthy", lambda: True, raising=False)

    client = TestClient(_ready_app())
    assert client.get("/health/ready").status_code == 401

    authorized = client.get(
        "/health/ready", headers={"Authorization": "Bearer secret"}
    )
    assert authorized.status_code == 200
    assert authorized.json()["status"] == "ok"


def test_health_ready_is_public_when_auth_is_disabled(monkeypatch) -> None:
    monkeypatch.setattr(config.security.auth, "enabled", False, raising=False)
    monkeypatch.setattr(health_manager, "is_healthy", lambda: True, raising=False)

    response = TestClient(_ready_app()).get("/health/ready")

    assert response.status_code == 200


def test_health_endpoint_remains_public_and_detailed(monkeypatch) -> None:
    monkeypatch.setattr(config.security.auth, "enabled", True, raising=False)
    monkeypatch.setattr(
        config.security.auth, "api_keys", [ApiKey(key="secret")], raising=False
    )
    monkeypatch.setattr(health_manager, "is_healthy", lambda: True, raising=False)

    response = TestClient(_public_app()).get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert "mcp_servers" in body
    assert "mcp_inventory" in body
