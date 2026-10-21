"""Container and Helm assets stay hardened (P2-06)."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_dockerfile_is_multi_stage_and_drops_compilers() -> None:
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert " AS builder" in text
    assert " AS runtime" in text
    builder, runtime = text.split(" AS runtime", 1)

    # Compilers build wheels, but must not ship.
    assert "build-essential" in builder
    assert "build-essential" not in runtime
    # Node stays: stdio servers commonly run through npx.
    assert "nodejs" in runtime
    # The healthcheck follows the configurable port and the liveness endpoint.
    assert "/health/live" in runtime
    assert "MCP_BRIDGE__NETWORK__PORT" in runtime
    assert "EXPOSE 11410" not in text


def test_helm_deployment_has_probes_and_a_hardened_context() -> None:
    text = (
        ROOT / "charts/mcp-bridge/templates/deployment.yaml"
    ).read_text(encoding="utf-8")

    assert "livenessProbe" in text
    assert "readinessProbe" in text
    assert "/health/live" in text
    assert "resources:" in text

    assert "runAsNonRoot: true" in text
    assert "readOnlyRootFilesystem: true" in text
    assert "allowPrivilegeEscalation: false" in text
    # A read-only root filesystem needs writable logs/cache/tmp mounts.
    assert text.count("emptyDir") >= 4
