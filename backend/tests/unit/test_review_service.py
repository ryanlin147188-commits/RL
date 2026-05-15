"""Unit tests for review_service — state machine logic with async mocks."""
from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.review import (
    ReviewAction,
    ReviewRecord,
    ReviewStatus,
    ReviewableEntityType,
)
from app.services import review_service


# ── ReviewStatus alias sanity ─────────────────────────────────────────


class TestReviewStatusAliases:
    def test_pending_is_in_review(self):
        assert ReviewStatus.PENDING == ReviewStatus.IN_REVIEW

    def test_approved_is_verified(self):
        assert ReviewStatus.APPROVED == ReviewStatus.VERIFIED

    def test_rejected_is_closed(self):
        assert ReviewStatus.REJECTED == ReviewStatus.CLOSED

    def test_string_values(self):
        assert ReviewStatus.PENDING.value == "InReview"
        assert ReviewStatus.APPROVED.value == "Verified"
        assert ReviewStatus.REJECTED.value == "Closed"


# ── _entity_label ─────────────────────────────────────────────────────


class TestEntityLabel:
    def _make_record(self, entity_type: ReviewableEntityType, entity_id: str) -> ReviewRecord:
        rec = MagicMock(spec=ReviewRecord)
        rec.entity_type = entity_type
        rec.entity_id = entity_id
        return rec

    def test_label_includes_type_and_truncated_id(self):
        rec = self._make_record(ReviewableEntityType.TESTCASE, "abcd1234-xxxx")
        label = review_service._entity_label(rec)
        assert "testcase" in label
        assert "abcd1234" in label

    def test_label_truncates_to_8_chars(self):
        rec = self._make_record(ReviewableEntityType.REPORT, "123456789abcdef")
        label = review_service._entity_label(rec)
        assert "12345678" in label
        assert "9abcdef" not in label


# ── approve() ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestApprove:
    def _pending_record(self) -> ReviewRecord:
        rec = MagicMock(spec=ReviewRecord)
        rec.status = ReviewStatus.PENDING
        rec.entity_type = ReviewableEntityType.TESTCASE
        rec.entity_id = "ent-001"
        rec.organization_id = "org-1"
        rec.submitted_by = "alice"
        return rec

    async def test_approve_pending_sets_approved(self):
        db = AsyncMock()
        db.flush = AsyncMock()
        rec = self._pending_record()
        with patch.object(review_service, "_append_history", AsyncMock()), \
             patch.object(review_service, "_notify_review_event", AsyncMock()):
            result = await review_service.approve(db, record=rec, reviewer="bob")
        assert result.status == ReviewStatus.APPROVED
        assert result.reviewed_by == "bob"

    async def test_approve_sets_reviewed_at(self):
        db = AsyncMock()
        db.flush = AsyncMock()
        rec = self._pending_record()
        before = datetime.utcnow()
        with patch.object(review_service, "_append_history", AsyncMock()), \
             patch.object(review_service, "_notify_review_event", AsyncMock()):
            await review_service.approve(db, record=rec, reviewer="bob")
        assert rec.reviewed_at is not None
        assert rec.reviewed_at >= before

    async def test_approve_non_pending_raises_400(self):
        from fastapi import HTTPException
        db = AsyncMock()
        rec = self._pending_record()
        rec.status = ReviewStatus.APPROVED
        with pytest.raises(HTTPException) as exc_info:
            await review_service.approve(db, record=rec, reviewer="bob")
        assert exc_info.value.status_code == 400

    async def test_approve_rejected_raises_400(self):
        from fastapi import HTTPException
        db = AsyncMock()
        rec = self._pending_record()
        rec.status = ReviewStatus.REJECTED
        with pytest.raises(HTTPException) as exc_info:
            await review_service.approve(db, record=rec, reviewer="bob")
        assert exc_info.value.status_code == 400


# ── reject() ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestReject:
    def _pending_record(self) -> ReviewRecord:
        rec = MagicMock(spec=ReviewRecord)
        rec.status = ReviewStatus.PENDING
        rec.entity_type = ReviewableEntityType.TESTCASE
        rec.entity_id = "ent-002"
        rec.organization_id = "org-1"
        rec.submitted_by = "alice"
        return rec

    async def test_reject_pending_sets_rejected(self):
        db = AsyncMock()
        db.flush = AsyncMock()
        rec = self._pending_record()
        with patch.object(review_service, "_append_history", AsyncMock()), \
             patch.object(review_service, "_notify_review_event", AsyncMock()):
            result = await review_service.reject(db, record=rec, reviewer="bob", reason="not ready")
        assert result.status == ReviewStatus.REJECTED
        assert result.current_reason == "not ready"

    async def test_reject_empty_reason_raises_400(self):
        from fastapi import HTTPException
        db = AsyncMock()
        rec = self._pending_record()
        with pytest.raises(HTTPException) as exc_info:
            await review_service.reject(db, record=rec, reviewer="bob", reason="")
        assert exc_info.value.status_code == 400

    async def test_reject_whitespace_reason_raises_400(self):
        from fastapi import HTTPException
        db = AsyncMock()
        rec = self._pending_record()
        with pytest.raises(HTTPException) as exc_info:
            await review_service.reject(db, record=rec, reviewer="bob", reason="   ")
        assert exc_info.value.status_code == 400

    async def test_reject_non_pending_raises_400(self):
        from fastapi import HTTPException
        db = AsyncMock()
        rec = self._pending_record()
        rec.status = ReviewStatus.APPROVED
        with pytest.raises(HTTPException) as exc_info:
            await review_service.reject(db, record=rec, reviewer="bob", reason="reason")
        assert exc_info.value.status_code == 400

    async def test_reject_stores_stripped_reason(self):
        db = AsyncMock()
        db.flush = AsyncMock()
        rec = self._pending_record()
        with patch.object(review_service, "_append_history", AsyncMock()), \
             patch.object(review_service, "_notify_review_event", AsyncMock()):
            await review_service.reject(db, record=rec, reviewer="bob", reason="  fix the typo  ")
        assert rec.current_reason == "fix the typo"


# ── revert() ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestRevert:
    def _approved_record(self) -> ReviewRecord:
        rec = MagicMock(spec=ReviewRecord)
        rec.status = ReviewStatus.APPROVED
        rec.entity_type = ReviewableEntityType.TESTCASE
        rec.entity_id = "ent-003"
        rec.organization_id = "org-1"
        rec.submitted_by = "alice"
        return rec

    async def test_revert_approved_to_pending(self):
        db = AsyncMock()
        db.flush = AsyncMock()
        rec = self._approved_record()
        with patch.object(review_service, "_append_history", AsyncMock()), \
             patch.object(review_service, "_notify_review_event", AsyncMock()):
            result = await review_service.revert(db, record=rec, actor="bob", reason="needs update")
        assert result.status == ReviewStatus.PENDING

    async def test_revert_rejected_to_pending(self):
        db = AsyncMock()
        db.flush = AsyncMock()
        rec = self._approved_record()
        rec.status = ReviewStatus.REJECTED
        with patch.object(review_service, "_append_history", AsyncMock()), \
             patch.object(review_service, "_notify_review_event", AsyncMock()):
            result = await review_service.revert(db, record=rec, actor="bob", reason="second look")
        assert result.status == ReviewStatus.PENDING

    async def test_revert_already_pending_raises_400(self):
        from fastapi import HTTPException
        db = AsyncMock()
        rec = self._approved_record()
        rec.status = ReviewStatus.PENDING
        with pytest.raises(HTTPException) as exc_info:
            await review_service.revert(db, record=rec, actor="bob", reason="reason")
        assert exc_info.value.status_code == 400

    async def test_revert_empty_reason_raises_400(self):
        from fastapi import HTTPException
        db = AsyncMock()
        rec = self._approved_record()
        with pytest.raises(HTTPException) as exc_info:
            await review_service.revert(db, record=rec, actor="bob", reason="")
        assert exc_info.value.status_code == 400

    async def test_revert_clears_reviewer_fields(self):
        db = AsyncMock()
        db.flush = AsyncMock()
        rec = self._approved_record()
        rec.reviewed_by = "old-reviewer"
        rec.reviewed_at = datetime.utcnow()
        with patch.object(review_service, "_append_history", AsyncMock()), \
             patch.object(review_service, "_notify_review_event", AsyncMock()):
            await review_service.revert(db, record=rec, actor="bob", reason="reason")
        assert rec.reviewed_by is None
        assert rec.reviewed_at is None


# ── is_locked() ───────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestIsLocked:
    async def test_approved_record_is_locked(self):
        db = AsyncMock()
        rec = MagicMock(spec=ReviewRecord)
        rec.status = ReviewStatus.APPROVED
        with patch.object(review_service, "get_record", AsyncMock(return_value=rec)):
            locked = await review_service.is_locked(
                db,
                entity_type=ReviewableEntityType.TESTCASE,
                entity_id="e1",
                organization_id="org-1",
            )
        assert locked is True

    async def test_pending_record_is_not_locked(self):
        db = AsyncMock()
        rec = MagicMock(spec=ReviewRecord)
        rec.status = ReviewStatus.PENDING
        with patch.object(review_service, "get_record", AsyncMock(return_value=rec)):
            locked = await review_service.is_locked(
                db,
                entity_type=ReviewableEntityType.TESTCASE,
                entity_id="e2",
                organization_id="org-1",
            )
        assert locked is False

    async def test_no_record_is_not_locked(self):
        db = AsyncMock()
        with patch.object(review_service, "get_record", AsyncMock(return_value=None)):
            locked = await review_service.is_locked(
                db,
                entity_type=ReviewableEntityType.TESTCASE,
                entity_id="e3",
                organization_id="org-1",
            )
        assert locked is False


# ── ensure_not_approved() ─────────────────────────────────────────────


@pytest.mark.asyncio
class TestEnsureNotApproved:
    async def test_not_locked_does_not_raise(self):
        db = AsyncMock()
        with patch.object(review_service, "is_locked", AsyncMock(return_value=False)):
            await review_service.ensure_not_approved(
                db,
                entity_type=ReviewableEntityType.TESTCASE,
                entity_id="e1",
                organization_id="org-1",
            )  # no exception

    async def test_locked_raises_423(self):
        from fastapi import HTTPException
        db = AsyncMock()
        with patch.object(review_service, "is_locked", AsyncMock(return_value=True)):
            with pytest.raises(HTTPException) as exc_info:
                await review_service.ensure_not_approved(
                    db,
                    entity_type=ReviewableEntityType.TESTCASE,
                    entity_id="e2",
                    organization_id="org-1",
                )
        assert exc_info.value.status_code == 423
