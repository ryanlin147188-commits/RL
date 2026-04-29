"""RBAC matrix tests for endpoints that have been wired to ``require_permission``.

Covers projects + defects + testcases as the representative set; new routers
added later should extend the parametrised matrix below rather than add a
new file.

Roles are seeded by ``app.main._seed_default_roles`` and have these
default permission sets:

    Viewer  -> read-only on every resource
    QA      -> read+write on testcase/defect/plan; read on project/wbs/...
    Admin   -> full access including project.delete + role.manage
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


# ── 1) Viewer cannot mutate ───────────────────────────────────────────────

async def test_viewer_can_list_projects(client, viewer_in_a) -> None:
    resp = await client.get("/api/projects", headers=viewer_in_a.headers)
    assert resp.status_code == 200


async def test_viewer_cannot_create_project(client, viewer_in_a) -> None:
    resp = await client.post(
        "/api/projects",
        json={"name": "viewer-shouldnt"},
        headers=viewer_in_a.headers,
    )
    assert resp.status_code == 403
    body = resp.json()
    assert body["detail"]["error"] == "permission_denied"
    assert "project.write" in body["detail"]["missing_permissions"]


async def test_viewer_cannot_delete_project(client, viewer_in_a, org_a) -> None:
    resp = await client.delete(
        f"/api/projects/{org_a.project_id}",
        headers=viewer_in_a.headers,
    )
    assert resp.status_code == 403


async def test_viewer_cannot_create_defect(client, viewer_in_a, org_a) -> None:
    resp = await client.post(
        "/api/defects",
        json={"project_id": org_a.project_id, "title": "viewer-shouldnt"},
        headers=viewer_in_a.headers,
    )
    assert resp.status_code == 403


# ── 2) QA can read+write defect/testcase but cannot delete project ────────

async def test_qa_can_create_defect(client, qa_in_a, org_a) -> None:
    resp = await client.post(
        "/api/defects",
        json={"project_id": org_a.project_id, "title": "qa-can"},
        headers=qa_in_a.headers,
    )
    assert resp.status_code == 201


async def test_qa_cannot_delete_project(client, qa_in_a, org_a) -> None:
    """Default QA role does not include project.delete."""
    resp = await client.delete(
        f"/api/projects/{org_a.project_id}",
        headers=qa_in_a.headers,
    )
    assert resp.status_code == 403
    assert "project.delete" in resp.json()["detail"]["missing_permissions"]


# ── 3) Admin role can do everything its role grants ───────────────────────

async def test_admin_can_create_project(client, admin_in_a) -> None:
    resp = await client.post(
        "/api/projects",
        json={"name": "admin-creating"},
        headers=admin_in_a.headers,
    )
    assert resp.status_code == 201


async def test_admin_can_delete_defect(client, admin_in_a, org_a) -> None:
    create = await client.post(
        "/api/defects",
        json={"project_id": org_a.project_id, "title": "to delete"},
        headers=admin_in_a.headers,
    )
    assert create.status_code == 201
    defect_id = create.json()["id"]
    resp = await client.delete(
        f"/api/defects/{defect_id}",
        headers=admin_in_a.headers,
    )
    assert resp.status_code == 204


# ── 4) User without a role still walks through permission checks ──────────

async def test_user_with_no_role_is_denied_writes(client, org_a) -> None:
    """A non-superuser whose ``role_id`` is NULL has zero permissions."""
    import uuid as _uuid
    from sqlalchemy import select

    from app.auth.security import create_access_token, hash_password
    from app.database import AsyncSessionLocal
    from app.models import User

    suffix = _uuid.uuid4().hex[:8]
    username = f"noroles-{suffix}"
    async with AsyncSessionLocal() as session:
        session.add(User(
            username=username,
            display_name=username,
            email=f"{username}@test.local",
            password_hash=hash_password("test-password-123"),
            role_id=None,
            organization_id=org_a.org_id,
            is_superuser=False,
            is_active=True,
        ))
        await session.commit()
    token = create_access_token(
        username, extra={"org_id": org_a.org_id, "is_superuser": False}
    )
    headers = {"Authorization": f"Bearer {token}"}

    resp = await client.post(
        "/api/projects", json={"name": "no-role"}, headers=headers
    )
    assert resp.status_code == 403


async def test_superuser_bypasses_permission_check(client, org_a) -> None:
    """Superusers route around require_permission entirely — by design, for
    self-hosted single-tenant deployments where one operator needs full access."""
    import uuid as _uuid

    from app.auth.security import create_access_token, hash_password
    from app.database import AsyncSessionLocal
    from app.models import User

    suffix = _uuid.uuid4().hex[:8]
    username = f"super-{suffix}"
    async with AsyncSessionLocal() as session:
        session.add(User(
            username=username,
            display_name=username,
            email=f"{username}@test.local",
            password_hash=hash_password("test-password-123"),
            role_id=None,
            organization_id=org_a.org_id,
            is_superuser=True,
            is_active=True,
        ))
        await session.commit()
    token = create_access_token(
        username, extra={"org_id": org_a.org_id, "is_superuser": True}
    )

    resp = await client.post(
        "/api/projects",
        json={"name": "super-can"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201
