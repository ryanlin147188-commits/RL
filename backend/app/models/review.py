"""Generic review / approval workflow ORM models.

Design:
  * One ``review_records`` row per (entity_type, entity_id, organization_id)
    captures the current state — what's pending / approved / rejected right
    now.
  * One ``review_history`` row per action (submit, approve, reject, revert)
    is appended for audit. Never updated, never deleted; the row count is
    the trail.

A separate "current state" table beats walking the history at every read
because the most common query is "is X approved?" and we need that on the
hot path of every PUT/DELETE.

Locking semantics live in the router/service layer
(:func:`app.services.review_service.is_locked`), not on the row itself —
the row just records ``status``.
"""
from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.auth.tenant import Assignable, TenantScoped

from .base import Base


class ReviewableEntityType(str, enum.Enum):
    """Entity classes that can be put through review (RFC-Review-1)。

    v1.1.9 加 DEFECT:缺陷管理項也能送審(由 QA / Lead 確認分類後關閉)。
    """

    TESTCASE = "testcase"
    DOCUMENT = "document"
    SCRIPT = "script"
    REPORT = "report"
    DEFECT = "defect"


class ReviewStatus(str, enum.Enum):
    """統一 7 值狀態 — 對齊 defect / todo / requirement。
    舊值 pending→IN_REVIEW, approved→VERIFIED, rejected→CLOSED 由 migration 0011 轉換。
    為相容舊呼叫方,保留 PENDING/APPROVED/REJECTED 別名指向新值。
    """
    NEW = "New"
    ASSIGNED = "Assigned"
    IN_PROGRESS = "InProgress"
    IN_REVIEW = "InReview"
    REWORK_REQUIRED = "ReworkRequired"
    VERIFIED = "Verified"
    CLOSED = "Closed"
    # 別名(指向新值)— 讓 review_service 內 ReviewStatus.PENDING / APPROVED / REJECTED 仍有效
    PENDING = IN_REVIEW
    APPROVED = VERIFIED
    REJECTED = CLOSED


class ReviewAction(str, enum.Enum):
    """Every state transition lands as one of these in ``review_history``."""

    SUBMIT = "submit"   # initial entry to pending (or re-submit after reject)
    APPROVE = "approve"
    REJECT = "reject"
    REVERT = "revert"   # approved -> pending so the entity is editable again


class ReviewRecord(Assignable, TenantScoped, Base):
    """The current review state of one entity. One row per (entity_type, entity_id)
    in a given org.
    """

    __tablename__ = "review_records"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "entity_type",
            "entity_id",
            name="uq_review_org_entity",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    entity_type: Mapped[ReviewableEntityType] = mapped_column(
        Enum(ReviewableEntityType, name="reviewable_entity_type"), nullable=False, index=True
    )
    entity_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[ReviewStatus] = mapped_column(
        Enum(ReviewStatus, name="review_status", values_callable=lambda x: [e.value for e in x], native_enum=False, length=20),
        nullable=False,
        default=ReviewStatus.PENDING,
        index=True,
    )
    # The most recent rejection / revert reason. Older reasons live in
    # review_history and are NOT overwritten there.
    current_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    submitted_by: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    submitted_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    reviewed_by: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class ReviewHistory(TenantScoped, Base):
    """Append-only audit trail. Every state-changing call to the review
    service writes one row here.
    """

    __tablename__ = "review_history"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    review_record_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("review_records.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    action: Mapped[ReviewAction] = mapped_column(
        Enum(ReviewAction, name="review_action"), nullable=False
    )
    actor: Mapped[str] = mapped_column(String(80), nullable=False)
    acted_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    # Required when action is REJECT or REVERT; nullable for SUBMIT/APPROVE.
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    previous_status: Mapped[Optional[ReviewStatus]] = mapped_column(
        Enum(ReviewStatus, name="review_status", values_callable=lambda x: [e.value for e in x], native_enum=False, length=20), nullable=True
    )
    new_status: Mapped[ReviewStatus] = mapped_column(
        Enum(ReviewStatus, name="review_status", values_callable=lambda x: [e.value for e in x], native_enum=False, length=20), nullable=False
    )
