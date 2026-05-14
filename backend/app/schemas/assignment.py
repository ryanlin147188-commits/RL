"""Assignment payload schemas (Phase 2)."""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class AssignableEntityType(str, Enum):
    """Mirrors the model classes that mix in `Assignable`. Kept distinct
    from the existing ReviewableEntityType enum (which is review-only)
    because `requirement` and `defect` aren't reviewable but ARE assignable."""

    REVIEW = "review"
    DEFECT = "defect"
    TESTCASE = "testcase"   # tree_nodes with level_type=TESTCASE
    REQUIREMENT = "requirement"


class AssigneeType(str, Enum):
    USER = "user"
    GROUP = "group"


class AssignRequest(BaseModel):
    entity_type: AssignableEntityType
    entity_id: str = Field(..., min_length=1, max_length=64)
    assignee: str = Field(..., min_length=1, max_length=80)
    assignee_type: AssigneeType = AssigneeType.USER


class AssignmentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    entity_type: AssignableEntityType
    entity_id: str
    assigned_to: Optional[str]
    assigned_to_type: Optional[str]
    assigned_by: Optional[str]
    assigned_at: Optional[datetime]
