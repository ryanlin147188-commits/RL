"""Generic assignment endpoint integration tests (Phase 2)."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.auth.security import hash_password
from app.database import AsyncSessionLocal
from app.models import (
    Defect,
    Notification,
    ReviewRecord,
    Requirement,
    TestDocument,
    TreeNode,
    User,
)
from app.models.tree_node import LevelType

pytestmark = pytest.mark.integration


# ── Helpers ───────────────────────────────────────────────────────────────

async def _seed_assignee_user(username: str, org) -> None:
    async with AsyncSessionLocal() as session:
        session.add(
            User(
                username=username,
                display_name=username,
                email=f"{username}@example.com",
                password_hash=hash_password("ignored"),
                organization_id=org.org_id,
                is_active=True,
                is_superuser=False,
            )
        )
        await session.commit()


async def _seed_testcase_node(org) -> str:
    async with AsyncSessionLocal() as session:
        node = TreeNode(
            id=str(uuid.uuid4()),
            project_id=org.project_id,
            organization_id=org.org_id,
            name="assignable-tc",
            level_type=LevelType.TESTCASE,
            sort_order=1,
        )
        session.add(node)
        await session.commit()
        return node.id


# ── Tests ─────────────────────────────────────────────────────────────────

async def test_assign_testcase_sets_fields_and_notifies(client, org_a) -> None:
    """POST /api/assignments fills the four columns + writes a Notification
    row for the assignee (in-app channel always)."""
    bob = f"bob_{uuid.uuid4().hex[:6]}"
    await _seed_assignee_user(bob, org_a)
    tc_id = await _seed_testcase_node(org_a)

    resp = await client.post(
        "/api/assignments",
        json={
            "entity_type": "testcase",
            "entity_id": tc_id,
            "assignee": bob,
            "assignee_type": "user",
        },
        headers=org_a.headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["assigned_to"] == bob
    assert body["assigned_to_type"] == "user"
    assert body["assigned_by"] == org_a.username
    assert body["assigned_at"] is not None

    # Verify the columns landed on the actual row
    async with AsyncSessionLocal() as db:
        n = await db.get(TreeNode, tc_id)
        assert n.assigned_to == bob

    # Verify the in-app notification was posted to bob
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(Notification).where(Notification.recipient == bob)
        )).scalars().all()
    assert any(
        r.event_key == "assignment.received" and r.related_entity_id == tc_id
        for r in rows
    )


async def test_unassign_clears_fields(client, org_a) -> None:
    bob = f"bob2_{uuid.uuid4().hex[:6]}"
    await _seed_assignee_user(bob, org_a)
    tc_id = await _seed_testcase_node(org_a)

    await client.post(
        "/api/assignments",
        json={
            "entity_type": "testcase",
            "entity_id": tc_id,
            "assignee": bob,
            "assignee_type": "user",
        },
        headers=org_a.headers,
    )
    resp = await client.delete(
        f"/api/assignments?entity_type=testcase&entity_id={tc_id}",
        headers=org_a.headers,
    )
    assert resp.status_code == 204

    async with AsyncSessionLocal() as db:
        n = await db.get(TreeNode, tc_id)
        assert n.assigned_to is None
        assert n.assigned_by is None
        assert n.assigned_at is None


async def test_assign_rejects_non_testcase_tree_node(client, org_a) -> None:
    """FEATURE/PLATFORM/PAGE/SCENARIO are not assignable -- only TESTCASE."""
    async with AsyncSessionLocal() as session:
        feat = TreeNode(
            id=str(uuid.uuid4()),
            project_id=org_a.project_id,
            organization_id=org_a.org_id,
            name="just-a-feat",
            level_type=LevelType.FEATURE,
            sort_order=1,
        )
        session.add(feat)
        await session.commit()
        feat_id = feat.id

    resp = await client.post(
        "/api/assignments",
        json={
            "entity_type": "testcase",
            "entity_id": feat_id,
            "assignee": "anyone",
            "assignee_type": "user",
        },
        headers=org_a.headers,
    )
    assert resp.status_code == 400
    assert "TESTCASE" in resp.json()["detail"]


async def test_list_my_assignments(client, org_a) -> None:
    bob = f"bob3_{uuid.uuid4().hex[:6]}"
    await _seed_assignee_user(bob, org_a)

    # Sign Bob in to get his bearer
    from app.auth.security import create_access_token
    bob_token = create_access_token(
        bob, extra={"org_id": org_a.org_id, "is_superuser": False}
    )
    bob_headers = {"Authorization": f"Bearer {bob_token}"}

    tc_id = await _seed_testcase_node(org_a)
    await client.post(
        "/api/assignments",
        json={
            "entity_type": "testcase",
            "entity_id": tc_id,
            "assignee": bob,
            "assignee_type": "user",
        },
        headers=org_a.headers,
    )

    listing = await client.get("/api/assignments/me", headers=bob_headers)
    assert listing.status_code == 200
    rows = listing.json()
    assert any(r["entity_id"] == tc_id and r["entity_type"] == "testcase" for r in rows)


async def test_assign_cross_tenant_returns_404(client, org_a, org_b) -> None:
    """Org B can't assign to an org-A testcase even if they know the id."""
    tc_id = await _seed_testcase_node(org_a)
    resp = await client.post(
        "/api/assignments",
        json={
            "entity_type": "testcase",
            "entity_id": tc_id,
            "assignee": "anyone",
            "assignee_type": "user",
        },
        headers=org_b.headers,
    )
    assert resp.status_code == 404
