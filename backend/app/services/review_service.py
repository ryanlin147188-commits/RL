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
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.review import (
    ReviewableEntityType,
    ReviewAction,
    ReviewHistory,
    ReviewRecord,
    ReviewStatus,
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
    return record


async def revert(
    db: AsyncSession,
    *,
    record: ReviewRecord,
    actor: str,
    reason: str,
) -> ReviewRecord:
    """Move an approved record back to pending so the underlying entity is
    editable again. Reason is required so the audit trail explains why."""
    if record.status != ReviewStatus.APPROVED:
        raise HTTPException(
            status_code=400,
            detail=f"can only revert an approved review (current: {record.status.value})",
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
