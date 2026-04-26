"""Test Milestone 測試時程 Pydantic Schemas。"""
from datetime import date, datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict


class MilestoneBase(BaseModel):
    name: str
    description: Optional[str] = None
    start_date: date
    end_date: date
    status: str = "Planned"
    owner: Optional[str] = None
    color: Optional[str] = None
    linked_test_round_id: Optional[str] = None
    linked_test_plan_id: Optional[str] = None


class MilestoneCreate(MilestoneBase):
    project_id: str


class MilestoneUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    status: Optional[str] = None
    owner: Optional[str] = None
    color: Optional[str] = None
    linked_test_round_id: Optional[str] = None
    linked_test_plan_id: Optional[str] = None


class MilestoneResponse(MilestoneBase):
    model_config = ConfigDict(from_attributes=True)
    id: str
    project_id: str
    created_at: datetime
    updated_at: datetime
