"""TestSchedule(測試時程)Pydantic schemas。"""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from app.models.test_schedule import TestScheduleStatus


class TestScheduleCreate(BaseModel):
    project_id: str = Field(..., min_length=1, max_length=36)
    name: str = Field(..., min_length=1, max_length=200)
    description: Optional[str] = None
    start_date: date
    end_date: date
    status: TestScheduleStatus = TestScheduleStatus.TODO
    color: str = Field(default="blue", max_length=20)
    progress: int = Field(default=0, ge=0, le=100)
    assigned_to: Optional[str] = Field(default=None, max_length=100)
    linked_target_type: Optional[str] = Field(default=None, max_length=32)
    linked_target_id: Optional[str] = Field(default=None, max_length=36)


class TestScheduleUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    description: Optional[str] = None
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    status: Optional[TestScheduleStatus] = None
    color: Optional[str] = Field(default=None, max_length=20)
    progress: Optional[int] = Field(default=None, ge=0, le=100)
    assigned_to: Optional[str] = Field(default=None, max_length=100)
    linked_target_type: Optional[str] = Field(default=None, max_length=32)
    linked_target_id: Optional[str] = Field(default=None, max_length=36)


class TestScheduleResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    project_id: str
    name: str
    description: Optional[str] = None
    start_date: date
    end_date: date
    status: TestScheduleStatus
    color: str
    progress: int = 0
    assigned_to: Optional[str] = None
    linked_target_type: Optional[str] = None
    linked_target_id: Optional[str] = None
    organization_id: Optional[str] = None
    created_at: datetime
    updated_at: datetime
