"""RFC-8: /healthz (liveness) + /readyz (readiness).

These endpoints exist for orchestrators (K8s, Docker swarm, ALB targets);
the test mostly exists so a refactor doesn't accidentally drop them.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


async def test_healthz_returns_ok_without_auth(client) -> None:
    """Liveness must NOT require a JWT — orchestrators don't carry one."""
    resp = await client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


async def test_readyz_checks_dependencies(client) -> None:
    """Readiness hits Postgres + Valkey — both up in the test stack."""
    resp = await client.get("/readyz")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ready"
    assert body["checks"]["postgres"] == "ok"
    assert body["checks"]["valkey"] == "ok"
