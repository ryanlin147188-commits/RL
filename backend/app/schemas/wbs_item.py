"""WBS (Work Breakdown Structure) Pydantic Schemas。"""
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict


class WbsItemBase(BaseModel):
    name: str
    description: Optional[str] = None
    parent_id: Optional[str] = None
    status: str = "NotStarted"
    progress: int = 0
    assignee: Optional[str] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    effort_hours: Optional[float] = None
    sort_order: int = 0


class WbsItemCreate(WbsItemBase):
    project_id: str
    code: Optional[str] = None  # 留空 → 自動產生 WBS-NNN


class WbsItemUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    parent_id: Optional[str] = None
    status: Optional[str] = None
    progress: Optional[int] = None
    assignee: Optional[str] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    effort_hours: Optional[float] = None
    sort_order: Optional[int] = None
    code: Optional[str] = None


class WbsItemResponse(WbsItemBase):
    model_config = ConfigDict(from_attributes=True)
    id: str
    project_id: str
    code: str
    created_at: datetime
    updated_at: datetime
