"""Review/approval workflow integration tests (RFC-Review-1).

Covers:
  * happy path: submit -> approve -> revert -> approve again
  * reject requires reason
  * approved entity is locked from edits (423)
  * reverting unlocks edits
  * audit history captures every transition with actor + timestamp
  * cross-tenant: org B cannot see / approve org A's review
"""
from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.integration


# ── Lifecycle ──────────────────────────────────────────────────────────

async def test_submit_creates_pending_record(client, org_a) -> None:
    resp = await client.post(
        "/api/reviews",
        json={"entity_type": "document", "entity_id": "doc-abc", "assignee": org_a.username, "assignee_type": "user"},
        headers=org_a.headers,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "InReview"
    assert body["submitted_by"] == org_a.username
    assert body["entity_type"] == "document"
    assert body["entity_id"] == "doc-abc"


async def test_submit_then_approve(client, org_a) -> None:
    submit = await client.post(
        "/api/reviews",
        json={"entity_type": "testcase", "entity_id": "tc-1", "assignee": org_a.username, "assignee_type": "user"},
        headers=org_a.headers,
    )
    record_id = submit.json()["id"]

    approve = await client.post(
        f"/api/reviews/{record_id}/approve", headers=org_a.headers
    )
    assert approve.status_code == 200
    body = approve.json()
    assert body["status"] == "Verified"
    assert body["reviewed_by"] == org_a.username
    assert body["reviewed_at"] is not None


async def test_reject_requires_reason(client, org_a) -> None:
    submit = await client.post(
        "/api/reviews",
        json={"entity_type": "script", "entity_id": "s-1", "assignee": org_a.username, "assignee_type": "user"},
        headers=org_a.headers,
    )
    record_id = submit.json()["id"]

    blank = await client.post(
        f"/api/reviews/{record_id}/reject",
        json={"reason": "   "},
        headers=org_a.headers,
    )
    # FastAPI validates min_length on the schema -> 422 from pydantic
    # OR app-level 400 from the service. Both shape it as a client error.
    assert blank.status_code in (400, 422)

    real = await client.post(
        f"/api/reviews/{record_id}/reject",
        json={"reason": "missing acceptance criteria"},
        headers=org_a.headers,
    )
    assert real.status_code == 200
    body = real.json()
    assert body["status"] == "Closed"
    assert body["current_reason"] == "missing acceptance criteria"


async def test_resubmit_after_reject(client, org_a) -> None:
    submit = await client.post(
        "/api/reviews",
        json={"entity_type": "report", "entity_id": "r-1", "assignee": org_a.username, "assignee_type": "user"},
        headers=org_a.headers,
    )
    record_id = submit.json()["id"]
    await client.post(
        f"/api/reviews/{record_id}/reject",
        json={"reason": "fix this"},
        headers=org_a.headers,
    )

    # Re-submitting the same entity_type+entity_id puts it back to pending.
    re = await client.post(
        "/api/reviews",
        json={"entity_type": "report", "entity_id": "r-1", "assignee": org_a.username, "assignee_type": "user"},
        headers=org_a.headers,
    )
    assert re.status_code == 201
    assert re.json()["status"] == "InReview"
    assert re.json()["current_reason"] is None


async def test_revert_rejected_back_to_pending(client, org_a) -> None:
    """A rejected review can be reverted to pending too -- lets the reviewer
    re-queue it without requiring the original submitter to re-submit."""
    submit = await client.post(
        "/api/reviews",
        json={"entity_type": "document", "entity_id": "d-rej-rev", "assignee": org_a.username, "assignee_type": "user"},
        headers=org_a.headers,
    )
    record_id = submit.json()["id"]
    await client.post(
        f"/api/reviews/{record_id}/reject",
        json={"reason": "first pass: missing AC"},
        headers=org_a.headers,
    )

    # Reason still required when reverting from rejected
    no_reason = await client.post(
        f"/api/reviews/{record_id}/revert",
        json={"reason": "   "},
        headers=org_a.headers,
    )
    assert no_reason.status_code in (400, 422)

    revert = await client.post(
        f"/api/reviews/{record_id}/revert",
        json={"reason": "submitter says they fixed it; let me re-look"},
        headers=org_a.headers,
    )
    assert revert.status_code == 200
    body = revert.json()
    assert body["status"] == "InReview"
    # current_reason now carries the revert reason, not the rejection reason
    assert "submitter says" in body["current_reason"]


async def test_revert_pending_is_400(client, org_a) -> None:
    """Reverting a pending review is a no-op error — there's nothing to undo."""
    submit = await client.post(
        "/api/reviews",
        json={"entity_type": "document", "entity_id": "d-already-pending", "assignee": org_a.username, "assignee_type": "user"},
        headers=org_a.headers,
    )
    record_id = submit.json()["id"]

    resp = await client.post(
        f"/api/reviews/{record_id}/revert",
        json={"reason": "trying anyway"},
        headers=org_a.headers,
    )
    assert resp.status_code == 400
    assert "already pending" in resp.json()["detail"]


async def test_revert_requires_reason_and_unlocks(client, org_a) -> None:
    submit = await client.post(
        "/api/reviews",
        json={"entity_type": "document", "entity_id": "d-rev", "assignee": org_a.username, "assignee_type": "user"},
        headers=org_a.headers,
    )
    record_id = submit.json()["id"]
    await client.post(f"/api/reviews/{record_id}/approve", headers=org_a.headers)

    blank = await client.post(
        f"/api/reviews/{record_id}/revert",
        json={"reason": ""},
        headers=org_a.headers,
    )
    assert blank.status_code in (400, 422)

    real = await client.post(
        f"/api/reviews/{record_id}/revert",
        json={"reason": "found a typo"},
        headers=org_a.headers,
    )
    assert real.status_code == 200
    body = real.json()
    assert body["status"] == "InReview"
    assert body["current_reason"] == "found a typo"


# ── Audit history ──────────────────────────────────────────────────────

async def test_history_captures_full_chain(client, org_a) -> None:
    submit = await client.post(
        "/api/reviews",
        json={"entity_type": "testcase", "entity_id": "tc-h", "assignee": org_a.username, "assignee_type": "user"},
        headers=org_a.headers,
    )
    record_id = submit.json()["id"]
    await client.post(
        f"/api/reviews/{record_id}/reject",
        json={"reason": "round one rejected"},
        headers=org_a.headers,
    )
    await client.post(
        "/api/reviews",
        json={"entity_type": "testcase", "entity_id": "tc-h", "assignee": org_a.username, "assignee_type": "user"},
        headers=org_a.headers,
    )
    await client.post(f"/api/reviews/{record_id}/approve", headers=org_a.headers)
    await client.post(
        f"/api/reviews/{record_id}/revert",
        json={"reason": "needs another look"},
        headers=org_a.headers,
    )

    history = await client.get(
        f"/api/reviews/{record_id}/history", headers=org_a.headers
    )
    assert history.status_code == 200
    actions = [h["action"] for h in history.json()]
    assert actions == ["submit", "reject", "submit", "approve", "revert"]
    # Timestamps + actors recorded on every entry
    for entry in history.json():
        assert entry["actor"] == org_a.username
        assert entry["acted_at"]


# ── Lock enforcement ───────────────────────────────────────────────────

async def test_approved_testcase_cannot_be_edited(client, org_a) -> None:
    """Approved testcase -> PUT returns 423 with `review_locked` error."""
    # Create a TreeNode + TestcaseContent so the route is reachable.
    from sqlalchemy import select
    from app.database import AsyncSessionLocal
    from app.models import TreeNode
    from app.models.tree_node import LevelType

    async with AsyncSessionLocal() as session:
        node = TreeNode(
            id=str(uuid.uuid4()),
            project_id=org_a.project_id,
            organization_id=org_a.org_id,
            name="case-locked",
            level_type=LevelType.TESTCASE,
            sort_order=1,
        )
        session.add(node)
        await session.commit()
        node_id = node.id

    # Submit + approve a review for this testcase
    submit = await client.post(
        "/api/reviews",
        json={"entity_type": "testcase", "entity_id": node_id, "assignee": org_a.username, "assignee_type": "user"},
        headers=org_a.headers,
    )
    rec = submit.json()["id"]
    await client.post(f"/api/reviews/{rec}/approve", headers=org_a.headers)

    # PUT now blocked
    put = await client.put(
        f"/api/testcases/{node_id}",
        json={"steps_json": [{"id": "1", "keyword": "Given", "action": "noop"}]},
        headers=org_a.headers,
    )
    assert put.status_code == 423
    body = put.json()
    assert body["detail"]["error"] == "review_locked"
    assert body["detail"]["entity_type"] == "testcase"


async def test_revert_unlocks_testcase(client, org_a) -> None:
    from sqlalchemy import select
    from app.database import AsyncSessionLocal
    from app.models import TreeNode
    from app.models.tree_node import LevelType

    async with AsyncSessionLocal() as session:
        node = TreeNode(
            id=str(uuid.uuid4()),
            project_id=org_a.project_id,
            organization_id=org_a.org_id,
            name="case-unlock",
            level_type=LevelType.TESTCASE,
            sort_order=1,
        )
        session.add(node)
        await session.commit()
        node_id = node.id

    submit = await client.post(
        "/api/reviews",
        json={"entity_type": "testcase", "entity_id": node_id, "assignee": org_a.username, "assignee_type": "user"},
        headers=org_a.headers,
    )
    rec = submit.json()["id"]
    await client.post(f"/api/reviews/{rec}/approve", headers=org_a.headers)

    # Approved -> 423
    blocked = await client.put(
        f"/api/testcases/{node_id}",
        json={"steps_json": []},
        headers=org_a.headers,
    )
    assert blocked.status_code == 423

    # Revert -> 200 again
    await client.post(
        f"/api/reviews/{rec}/revert",
        json={"reason": "tweaks needed"},
        headers=org_a.headers,
    )
    ok = await client.put(
        f"/api/testcases/{node_id}",
        json={"steps_json": []},
        headers=org_a.headers,
    )
    assert ok.status_code == 200


# ── Cross-tenant isolation ────────────────────────────────────────────

async def test_org_b_cannot_see_org_a_reviews(client, org_a, org_b) -> None:
    submit = await client.post(
        "/api/reviews",
        json={"entity_type": "document", "entity_id": "secret-doc", "assignee": org_a.username, "assignee_type": "user"},
        headers=org_a.headers,
    )
    record_id = submit.json()["id"]

    # B can't read it
    resp = await client.get(f"/api/reviews/{record_id}", headers=org_b.headers)
    assert resp.status_code == 404

    # B's list doesn't include it
    listing = await client.get("/api/reviews", headers=org_b.headers)
    assert all(r["id"] != record_id for r in listing.json())

    # B can't approve it
    approve = await client.post(
        f"/api/reviews/{record_id}/approve", headers=org_b.headers
    )
    assert approve.status_code == 404


# ── Permissions: viewer can't approve ─────────────────────────────────

# ── Auto-create on insert ─────────────────────────────────────────────

async def test_creating_testcase_node_autocreates_pending_review(client, org_a) -> None:
    """A new TreeNode(level_type=TESTCASE) lands a pending ReviewRecord
    without anyone calling POST /api/reviews."""
    import uuid as _uuid
    from sqlalchemy import select

    from app.database import AsyncSessionLocal
    from app.models import TreeNode
    from app.models.tree_node import LevelType
    from app.auth.context import current_org_id, current_username

    org_token = current_org_id.set(org_a.org_id)
    user_token = current_username.set(org_a.username)
    try:
        async with AsyncSessionLocal() as session:
            node = TreeNode(
                id=str(_uuid.uuid4()),
                project_id=org_a.project_id,
                organization_id=org_a.org_id,
                name="auto-review-case",
                level_type=LevelType.TESTCASE,
                sort_order=1,
            )
            session.add(node)
            await session.commit()
            node_id = node.id
    finally:
        current_username.reset(user_token)
        current_org_id.reset(org_token)

    # Now the review center should list it (pending tab) without us
    # calling POST /api/reviews.
    listing = await client.get("/api/reviews?status=pending", headers=org_a.headers)
    assert listing.status_code == 200
    rows = listing.json()
    assert any(
        r["entity_type"] == "testcase" and r["entity_id"] == node_id and r["status"] == "InReview"
        for r in rows
    ), f"expected auto-created review for {node_id}, got {rows}"


async def test_deleting_entity_cascade_deletes_review(client, org_a) -> None:
    """Deleting a TreeNode (testcase) must also drop its ReviewRecord -- the
    review center should never list a row whose entity is gone."""
    import uuid as _uuid

    from sqlalchemy import select
    from app.database import AsyncSessionLocal
    from app.models import ReviewRecord, TreeNode
    from app.models.tree_node import LevelType
    from app.auth.context import current_org_id, current_username

    org_token = current_org_id.set(org_a.org_id)
    user_token = current_username.set(org_a.username)
    try:
        async with AsyncSessionLocal() as session:
            node = TreeNode(
                id=str(_uuid.uuid4()),
                project_id=org_a.project_id,
                organization_id=org_a.org_id,
                name="cascade-target",
                level_type=LevelType.TESTCASE,
                sort_order=1,
            )
            session.add(node)
            await session.commit()
            node_id = node.id

        # Sanity: review row was auto-created
        async with AsyncSessionLocal() as session:
            rec = (
                await session.execute(
                    select(ReviewRecord).where(ReviewRecord.entity_id == node_id)
                )
            ).scalar_one_or_none()
            assert rec is not None, "expected auto-created ReviewRecord"

        # Now delete the TreeNode and verify the ReviewRecord follows.
        async with AsyncSessionLocal() as session:
            n = await session.get(TreeNode, node_id)
            await session.delete(n)
            await session.commit()

        async with AsyncSessionLocal() as session:
            rec = (
                await session.execute(
                    select(ReviewRecord).where(ReviewRecord.entity_id == node_id)
                )
            ).scalar_one_or_none()
            assert rec is None, "ReviewRecord should have been cascade-deleted"
    finally:
        current_username.reset(user_token)
        current_org_id.reset(org_token)


async def test_creating_non_testcase_node_does_not_autocreate(client, org_a) -> None:
    """FEATURE/PLATFORM/PAGE/SCENARIO are organizational containers, not
    reviewable. They must NOT spawn review records."""
    import uuid as _uuid

    from app.database import AsyncSessionLocal
    from app.models import TreeNode
    from app.models.tree_node import LevelType
    from app.auth.context import current_org_id, current_username

    org_token = current_org_id.set(org_a.org_id)
    user_token = current_username.set(org_a.username)
    try:
        async with AsyncSessionLocal() as session:
            node = TreeNode(
                id=str(_uuid.uuid4()),
                project_id=org_a.project_id,
                organization_id=org_a.org_id,
                name="just-a-feature",
                level_type=LevelType.FEATURE,
                sort_order=1,
            )
            session.add(node)
            await session.commit()
            node_id = node.id
    finally:
        current_username.reset(user_token)
        current_org_id.reset(org_token)

    listing = await client.get("/api/reviews", headers=org_a.headers)
    rows = listing.json()
    assert not any(r["entity_id"] == node_id for r in rows)


async def test_viewer_cannot_approve(client, org_a, viewer_in_a) -> None:
    submit = await client.post(
        "/api/reviews",
        json={"entity_type": "document", "entity_id": "vw-d", "assignee": org_a.username, "assignee_type": "user"},
        headers=org_a.headers,   # admin submits
    )
    record_id = submit.json()["id"]

    # Viewer (in same org) can SEE but not APPROVE.
    seen = await client.get(f"/api/reviews/{record_id}", headers=viewer_in_a.headers)
    assert seen.status_code == 200

    blocked = await client.post(
        f"/api/reviews/{record_id}/approve", headers=viewer_in_a.headers
    )
    assert blocked.status_code == 403


# ── Notification dispatch (Phase 3 wiring) ─────────────────────────────

async def test_approve_notifies_submitter(client, org_a) -> None:
    """Approving a record fires a `review.approved` Notification to the
    original submitter so they know their work cleared."""
    from sqlalchemy import select
    from app.database import AsyncSessionLocal
    from app.models import Notification

    submit = await client.post(
        "/api/reviews",
        json={"entity_type": "document", "entity_id": "doc-notify-1", "assignee": org_a.username, "assignee_type": "user"},
        headers=org_a.headers,
    )
    record_id = submit.json()["id"]
    await client.post(
        f"/api/reviews/{record_id}/approve", headers=org_a.headers
    )

    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(Notification).where(Notification.recipient == org_a.username)
        )).scalars().all()
    assert any(
        r.event_key == "review.approved" and r.related_entity_id == "doc-notify-1"
        for r in rows
    ), "expected a review.approved notification for the submitter"


async def test_reject_notifies_submitter_with_reason(client, org_a) -> None:
    from sqlalchemy import select
    from app.database import AsyncSessionLocal
    from app.models import Notification

    submit = await client.post(
        "/api/reviews",
        json={"entity_type": "document", "entity_id": "doc-notify-2", "assignee": org_a.username, "assignee_type": "user"},
        headers=org_a.headers,
    )
    record_id = submit.json()["id"]
    await client.post(
        f"/api/reviews/{record_id}/reject",
        json={"reason": "缺失欄位"},
        headers=org_a.headers,
    )

    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(Notification).where(
                Notification.recipient == org_a.username,
                Notification.event_key == "review.rejected",
                Notification.related_entity_id == "doc-notify-2",
            )
        )).scalars().all()
    assert len(rows) == 1
    assert "缺失欄位" in (rows[0].body or "")
