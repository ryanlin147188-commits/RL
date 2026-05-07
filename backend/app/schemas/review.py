"""Review/approval workflow Pydantic schemas."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from app.models.review import ReviewableEntityType, ReviewAction, ReviewStatus


class SubmitReviewRequest(BaseModel):
    entity_type: ReviewableEntityType
    entity_id: str = Field(..., min_length=1, max_length=64)
    # 送審必選審核者 — 一般使用者(`user`)或群組(`group`)
    assignee: str = Field(..., min_length=1, max_length=80)
    assignee_type: str = Field(default="user", pattern="^(user|group)$")


class RejectReviewRequest(BaseModel):
    reason: str = Field(..., min_length=1, max_length=2000)


class RevertReviewRequest(BaseModel):
    reason: str = Field(..., min_length=1, max_length=2000)


class ReviewRecordResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    entity_type: ReviewableEntityType
    entity_id: str
    # Human-readable name for the entity (testcase name, document title,
    # recording target_url, report task_id...). Populated by the router on
    # read so the UI doesn't have to N+1-fetch each entity. Nullable when
    # the underlying entity has been deleted.
    entity_name: Optional[str] = None
    status: ReviewStatus
    current_reason: Optional[str]
    submitted_by: Optional[str]
    submitted_at: datetime
    reviewed_by: Optional[str]
    reviewed_at: Optional[datetime]
    # Phase 2 — assignment metadata. Populated when an admin/owner calls
    # POST /api/assignments; nullable when nobody owns the review yet.
    # The dispatch pushes review.submitted notifications to assigned_to.
    assigned_to: Optional[str] = None
    assigned_to_type: Optional[str] = None
    assigned_by: Optional[str] = None
    assigned_at: Optional[datetime] = None
    organization_id: Optional[str]
    created_at: datetime
    updated_at: datetime


class ReviewHistoryEntry(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    review_record_id: str
    action: ReviewAction
    actor: str
    acted_at: datetime
    reason: Optional[str]
    previous_status: Optional[ReviewStatus]
    new_status: ReviewStatus
