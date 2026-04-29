"""IDOR (cross-tenant access) regression tests.

These prove that a user authenticated for organisation B cannot read or
mutate resources that belong to organisation A — the central guarantee of
Layer 1 multi-tenancy.

Failure must be 404, not 403, on read paths so the API does not leak the
existence of resources across tenants. Write paths may be 404 OR 403
depending on the helper used by that router (``ensure_project_writable``
deliberately chose 403 to differentiate from "doesn't exist"). The tests
accept both for write paths.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


async def test_org_b_cannot_list_org_a_projects(client, org_a, org_b) -> None:
    """Org B's project list must not include any of org A's projects."""
    resp = await client.get("/api/projects", headers=org_b.headers)
    assert resp.status_code == 200
    seen_ids = {p["id"] for p in resp.json()}
    assert org_a.project_id not in seen_ids


async def test_org_b_get_org_a_project_returns_404(client, org_a, org_b) -> None:
    resp = await client.get(f"/api/projects/{org_a.project_id}", headers=org_b.headers)
    assert resp.status_code == 404, (
        "cross-tenant GET must 404 (not 403, to avoid leaking existence)"
    )


async def test_org_b_get_org_a_tree_returns_404(client, org_a, org_b) -> None:
    resp = await client.get(
        f"/api/projects/{org_a.project_id}/tree", headers=org_b.headers
    )
    assert resp.status_code == 404


async def test_org_b_update_org_a_project_returns_404(client, org_a, org_b) -> None:
    resp = await client.put(
        f"/api/projects/{org_a.project_id}",
        json={"name": "hijacked"},
        headers=org_b.headers,
    )
    assert resp.status_code in (403, 404)


async def test_org_b_delete_org_a_project_returns_404(client, org_a, org_b) -> None:
    resp = await client.delete(
        f"/api/projects/{org_a.project_id}", headers=org_b.headers
    )
    assert resp.status_code in (403, 404)


async def test_unauthenticated_cannot_list_projects(client) -> None:
    """Sanity: AuthMiddleware blocks anonymous before scope checks even run."""
    resp = await client.get("/api/projects")
    assert resp.status_code == 401


async def test_org_a_can_still_see_own_project(client, org_a) -> None:
    """Don't break the happy path while locking the door."""
    resp = await client.get(f"/api/projects/{org_a.project_id}", headers=org_a.headers)
    assert resp.status_code == 200
    assert resp.json()["id"] == org_a.project_id
