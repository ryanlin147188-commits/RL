"""Review/approval workflow service.

Centralises all state transitions so routers don't reach into the ORM.
Every transition writes a ``ReviewHistory`` row alongside the
``ReviewRecord`` mutation in the same transaction — there is no path that
mutates the record without a corresponding audit row.

State machine (illegal transitions raise 400):

       SUBMIT       APPROVE
   ┌──────────►pending───────►approved
   │              │  ▲           │
   │       REJECT │  │REVERT     │
   │              ▼  │           │
   └─────────rejected ◄──────────┘
              SUBMIT (re-submit re-enters pending)
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from fastapi import HTTPException
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.auth.context import current_org_id, current_username
from app.models.review import (
    ReviewableEntityType,
    ReviewAction,
    ReviewHistory,
    ReviewRecord,
    ReviewStatus,
)
from app.services.notification_dispatch import notify


def _entity_label(record: ReviewRecord) -> str:
    """Short human label used in notification titles/bodies."""
    return f"{record.entity_type.value} {record.entity_id[:8]}"


async def _notify_review_event(
    db: AsyncSession,
    *,
    record: ReviewRecord,
    event_key: str,
    recipient: Optional[str],
    title: str,
    body: str,
    level: str = "info",
) -> None:
    """Wrap notify() with the review-specific payload. Silently skips if
    there's no recipient (e.g. submitted but nobody assigned to review)."""
    if not recipient:
        return
    await notify(
        db=db,
        event_key=event_key,
        recipient=recipient,
        title=title,
        body=body,
        level=level,
        related_entity_type=record.entity_type.value,
        related_entity_id=record.entity_id,
        organization_id=record.organization_id,
    )


# ── Internal helpers ────────────────────────────────────────────────────

async def _append_history(
    db: AsyncSession,
    *,
    record: ReviewRecord,
    action: ReviewAction,
    actor: str,
    reason: Optional[str],
    previous_status: Optional[ReviewStatus],
    new_status: ReviewStatus,
) -> None:
    db.add(
        ReviewHistory(
            review_record_id=record.id,
            organization_id=record.organization_id,
            action=action,
            actor=actor,
            reason=reason,
            previous_status=previous_status,
            new_status=new_status,
        )
    )


# ── Public API ──────────────────────────────────────────────────────────

async def get_record(
    db: AsyncSession,
    *,
    entity_type: ReviewableEntityType,
    entity_id: str,
    organization_id: Optional[str],
) -> Optional[ReviewRecord]:
    stmt = select(ReviewRecord).where(
        ReviewRecord.entity_type == entity_type,
        ReviewRecord.entity_id == entity_id,
    )
    if organization_id is not None:
        stmt = stmt.where(ReviewRecord.organization_id == organization_id)
    return (await db.execute(stmt)).scalar_one_or_none()


async def is_locked(
    db: AsyncSession,
    *,
    entity_type: ReviewableEntityType,
    entity_id: str,
    organization_id: Optional[str],
) -> bool:
    """Hot-path helper for routers' write guards.

    Returns True iff the entity has a review record in the APPROVED state.
    Pending / rejected / no-record-at-all = unlocked.
    """
    record = await get_record(
        db,
        entity_type=entity_type,
        entity_id=entity_id,
        organization_id=organization_id,
    )
    return record is not None and record.status == ReviewStatus.APPROVED


async def submit(
    db: AsyncSession,
    *,
    entity_type: ReviewableEntityType,
    entity_id: str,
    submitted_by: str,
    organization_id: Optional[str],
) -> ReviewRecord:
    """Create a pending review for the entity, OR re-submit a previously
    rejected one back into pending (rejected -> pending allowed)."""
    existing = await get_record(
        db,
        entity_type=entity_type,
        entity_id=entity_id,
        organization_id=organization_id,
    )
    now = datetime.utcnow()

    if existing is None:
        record = ReviewRecord(
            entity_type=entity_type,
            entity_id=entity_id,
            status=ReviewStatus.PENDING,
            submitted_by=submitted_by,
            submitted_at=now,
            organization_id=organization_id,
        )
        db.add(record)
        await db.flush()
        await _append_history(
            db,
            record=record,
            action=ReviewAction.SUBMIT,
            actor=submitted_by,
            reason=None,
            previous_status=None,
            new_status=ReviewStatus.PENDING,
        )
        await db.flush()
        await _notify_review_event(
            db,
            record=record,
            event_key="review.submitted",
            recipient=getattr(record, "assigned_to", None),
            title=f"待您審核：{_entity_label(record)}",
            body=f"{submitted_by} 提交了一筆 {record.entity_type.value} 等待審核。",
        )
        return record

    if existing.status == ReviewStatus.PENDING:
        # Idempotent: already pending, no state change but no error either.
        return existing
    if existing.status == ReviewStatus.APPROVED:
        raise HTTPException(
            status_code=409,
            detail="entity is already approved; revert it first if you want to re-submit",
        )

    # rejected -> pending: re-entry path
    prev = existing.status
    existing.status = ReviewStatus.PENDING
    existing.submitted_by = submitted_by
    existing.submitted_at = now
    existing.current_reason = None
    existing.reviewed_by = None
    existing.reviewed_at = None
    await _append_history(
        db,
        record=existing,
        action=ReviewAction.SUBMIT,
        actor=submitted_by,
        reason=None,
        previous_status=prev,
        new_status=ReviewStatus.PENDING,
    )
    await db.flush()
    await _notify_review_event(
        db,
        record=existing,
        event_key="review.submitted",
        recipient=getattr(existing, "assigned_to", None),
        title=f"待您審核：{_entity_label(existing)}",
        body=f"{submitted_by} 重新提交了一筆 {existing.entity_type.value} 等待審核。",
    )
    return existing


async def approve(
    db: AsyncSession,
    *,
    record: ReviewRecord,
    reviewer: str,
) -> ReviewRecord:
    if record.status != ReviewStatus.PENDING:
        raise HTTPException(
            status_code=400,
            detail=f"can only approve a pending review (current: {record.status.value})",
        )
    prev = record.status
    record.status = ReviewStatus.APPROVED
    record.reviewed_by = reviewer
    record.reviewed_at = datetime.utcnow()
    record.current_reason = None
    await _append_history(
        db,
        record=record,
        action=ReviewAction.APPROVE,
        actor=reviewer,
        reason=None,
        previous_status=prev,
        new_status=ReviewStatus.APPROVED,
    )
    await db.flush()
    await _notify_review_event(
        db,
        record=record,
        event_key="review.approved",
        recipient=record.submitted_by,
        title=f"已通過：{_entity_label(record)}",
        body=f"{reviewer} 已通過您送出的審核。",
        level="success",
    )
    return record


async def reject(
    db: AsyncSession,
    *,
    record: ReviewRecord,
    reviewer: str,
    reason: str,
) -> ReviewRecord:
    if record.status != ReviewStatus.PENDING:
        raise HTTPException(
            status_code=400,
            detail=f"can only reject a pending review (current: {record.status.value})",
        )
    if not reason or not reason.strip():
        raise HTTPException(status_code=400, detail="rejection requires a reason")
    prev = record.status
    record.status = ReviewStatus.REJECTED
    record.reviewed_by = reviewer
    record.reviewed_at = datetime.utcnow()
    record.current_reason = reason.strip()
    await _append_history(
        db,
        record=record,
        action=ReviewAction.REJECT,
        actor=reviewer,
        reason=reason.strip(),
        previous_status=prev,
        new_status=ReviewStatus.REJECTED,
    )
    await db.flush()
    await _notify_review_event(
        db,
        record=record,
        event_key="review.rejected",
        recipient=record.submitted_by,
        title=f"已退回：{_entity_label(record)}",
        body=f"{reviewer} 退回您送出的審核。原因：{reason.strip()}",
        level="warning",
    )
    return record


async def revert(
    db: AsyncSession,
    *,
    record: ReviewRecord,
    actor: str,
    reason: str,
) -> ReviewRecord:
    """Move an approved or rejected record back to pending so it can flow
    through the queue again. Reason is required so the audit trail explains
    why the reviewer reopened the case (a rejected entity might warrant a
    second look after the submitter pushed a fix; an approved one might need
    re-review after a regression).

    Pending -> pending is a no-op error (nothing to revert)."""
    if record.status == ReviewStatus.PENDING:
        raise HTTPException(
            status_code=400,
            detail="review is already pending; nothing to revert",
        )
    if not reason or not reason.strip():
        raise HTTPException(status_code=400, detail="revert requires a reason")
    prev = record.status
    record.status = ReviewStatus.PENDING
    record.current_reason = reason.strip()
    record.reviewed_by = None
    record.reviewed_at = None
    await _append_history(
        db,
        record=record,
        action=ReviewAction.REVERT,
        actor=actor,
        reason=reason.strip(),
        previous_status=prev,
        new_status=ReviewStatus.PENDING,
    )
    await db.flush()
    await _notify_review_event(
        db,
        record=record,
        event_key="review.reverted",
        recipient=record.submitted_by,
        title=f"已退回審核：{_entity_label(record)}",
        body=f"{actor} 將此審核退回待審核狀態。原因：{reason.strip()}",
        level="info",
    )
    return record


# ── Lock guard for write-side routers ──────────────────────────────────

async def ensure_not_approved(
    db: AsyncSession,
    *,
    entity_type: ReviewableEntityType,
    entity_id: str,
    organization_id: Optional[str],
) -> None:
    """Raise 423 Locked if the entity's review is in APPROVED state.

    Routers handling PUT/DELETE on reviewable entities call this before
    proceeding. 423 is the right code per RFC 4918: "the source or
    destination resource is locked"; clients can disambiguate from 403/404.
    """
    if await is_locked(
        db,
        entity_type=entity_type,
        entity_id=entity_id,
        organization_id=organization_id,
    ):
        raise HTTPException(
            status_code=423,
            detail={
                "error": "review_locked",
                "message": "this entity is approved; revert its review before editing",
                "entity_type": entity_type.value,
                "entity_id": entity_id,
            },
        )


# ── Auto-create on insert (RFC-Review-1 phase 2) ──────────────────────
# Hook into SQLAlchemy's flush so that a freshly-created TreeNode (testcase),
# TestDocument, RecordingSession, or ExecutionReport automatically lands in
# the "pending" review queue. This means everything new shows up in the
# Review Center without anyone having to remember to call POST /api/reviews.

def _kwargs_for_review(obj: Any) -> Optional[dict]:
    """Return ReviewRecord kwargs for `obj` if it should auto-spawn a review,
    else None. Imports are local so the SQLAlchemy event registration in
    install_review_autocreate() does not pull all 33 model files at module
    load time.

    Side effect: if `obj.id` is unset, generate one inline. SQLAlchemy's
    ``default=lambda: str(uuid.uuid4())`` fires during INSERT compilation,
    which is *after* before_flush -- so without this assignment we'd build
    a ReviewRecord with entity_id=None and the FK/NOT NULL would explode.
    Setting it here means SQLAlchemy uses our value verbatim at INSERT time.
    """
    import uuid as _uuid

    from app.models.execution_report import ExecutionReport
    from app.models.recording import RecordingSession
    from app.models.test_document import TestDocument
    from app.models.tree_node import LevelType, TreeNode

    def _ensure_id(o: Any) -> str:
        if not getattr(o, "id", None):
            o.id = str(_uuid.uuid4())
        return o.id

    if isinstance(obj, TreeNode):
        # Only TESTCASE-level nodes get reviewed; FEATURE/PLATFORM/PAGE/SCENARIO
        # are organizational containers, not user-authored content.
        if obj.level_type == LevelType.TESTCASE:
            return {"entity_type": ReviewableEntityType.TESTCASE, "entity_id": _ensure_id(obj)}
        return None
    if isinstance(obj, TestDocument):
        return {"entity_type": ReviewableEntityType.DOCUMENT, "entity_id": _ensure_id(obj)}
    if isinstance(obj, RecordingSession):
        return {"entity_type": ReviewableEntityType.SCRIPT, "entity_id": _ensure_id(obj)}
    if isinstance(obj, ExecutionReport):
        return {"entity_type": ReviewableEntityType.REPORT, "entity_id": _ensure_id(obj)}
    return None


def install_review_autocreate() -> None:
    """Register the before_flush hook that:
      * auto-spawns pending review records for newly-created reviewable entities
      * cascade-deletes the matching review record when an entity is deleted

    Idempotent (called once at app boot from app.database).

    Failure handling: any error inside the hook is logged but **never**
    propagated -- a misconfigured review pipeline must not break the
    user's actual write/delete. The worst case is a missing or stale
    review row, which is far better than 500-ing a routine CRUD call.
    """
    import logging
    log = logging.getLogger(__name__)

    @event.listens_for(Session, "before_flush")
    def _autocreate(session: Session, flush_context: Any, instances: Any) -> None:  # noqa: ARG001
        try:
            org_id = current_org_id.get()
            username = current_username.get() or "system"
            # Without a tenant context we have no idea who owns this row;
            # falling through means rows still flush without a review record,
            # which is preferable to crashing the request.
            if not org_id:
                return

            already_queued: set[tuple[str, str]] = {
                (r.entity_type.value if hasattr(r.entity_type, "value") else r.entity_type, r.entity_id)
                for r in session.new
                if isinstance(r, ReviewRecord)
            }

            to_add: list[ReviewRecord] = []
            for obj in list(session.new):
                kwargs = _kwargs_for_review(obj)
                if not kwargs:
                    continue
                key = (kwargs["entity_type"].value, kwargs["entity_id"])
                if key in already_queued:
                    continue
                already_queued.add(key)
                to_add.append(
                    ReviewRecord(
                        organization_id=org_id,
                        submitted_by=username,
                        submitted_at=datetime.utcnow(),
                        status=ReviewStatus.PENDING,
                        **kwargs,
                    )
                )

            for rec in to_add:
                session.add(rec)
        except Exception:  # noqa: BLE001
            log.exception("review autocreate hook failed; the user's write proceeds without a review record")

    @event.listens_for(Session, "before_flush")
    def _autodelete(session: Session, flush_context: Any, instances: Any) -> None:  # noqa: ARG001
        """Cascade-delete the ReviewRecord when its underlying entity is
        deleted. Without this the review center would keep listing rows
        whose entity no longer exists ("實體已刪除") -- ops feedback says
        that's noise, not signal.

        The DB's FK ON DELETE CASCADE handles ReviewHistory automatically.
        """
        try:
            to_delete: list[ReviewRecord] = []
            for obj in list(session.deleted):
                if isinstance(obj, ReviewRecord):
                    continue
                kwargs = _kwargs_for_review(obj)
                if not kwargs:
                    continue
                # Event handler runs in sync greenlet context; use the
                # synchronous Session API even when the originating engine
                # is async (the bridge unwraps it for us).
                rec = session.scalars(
                    select(ReviewRecord).where(
                        ReviewRecord.entity_type == kwargs["entity_type"],
                        ReviewRecord.entity_id == kwargs["entity_id"],
                    )
                ).first()
                if rec is not None:
                    to_delete.append(rec)
            for rec in to_delete:
                session.delete(rec)
        except Exception:  # noqa: BLE001
            log.exception("review cascade-delete hook failed; review row may be left dangling")
