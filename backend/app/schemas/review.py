"""Review/approval workflow Pydantic schemas."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from app.models.review import ReviewableEntityType, ReviewAction, ReviewStatus


class SubmitReviewRequest(BaseModel):
    entity_type: ReviewableEntityType
    entity_id: str = Field(..., min_length=1, max_length=64)


class RejectReviewRequest(BaseModel):
    reason: str = Field(..., min_length=1, max_length=2000)


class RevertReviewRequest(BaseModel):
    reason: str = Field(..., min_length=1, max_length=2000)


class ReviewRecordResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    entity_type: ReviewableEntityType
    entity_id: str
    status: ReviewStatus
    current_reason: Optional[str]
    submitted_by: Optional[str]
    submitted_at: datetime
    reviewed_by: Optional[str]
    reviewed_at: Optional[datetime]
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
