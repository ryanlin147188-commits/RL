"""Test Plan 測試計畫 Pydantic Schemas。"""
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict


class TestPlanBase(BaseModel):
    title: str
    version: Optional[str] = None
    scope_in_text: Optional[str] = None
    scope_out_text: Optional[str] = None
    test_strategy_text: Optional[str] = None
    resources_text: Optional[str] = None
    schedule_text: Optional[str] = None
    risks_text: Optional[str] = None
    entry_criteria_json: Optional[Any] = None
    exit_criteria_json: Optional[Any] = None
    approvals_json: Optional[Any] = None
    status: str = "Draft"
    owner: Optional[str] = None


class TestPlanCreate(TestPlanBase):
    project_id: str
    code: Optional[str] = None  # 留空 → 自動產 TP-NNN


class TestPlanUpdate(BaseModel):
    title: Optional[str] = None
    version: Optional[str] = None
    scope_in_text: Optional[str] = None
    scope_out_text: Optional[str] = None
    test_strategy_text: Optional[str] = None
    resources_text: Optional[str] = None
    schedule_text: Optional[str] = None
    risks_text: Optional[str] = None
    entry_criteria_json: Optional[Any] = None
    exit_criteria_json: Optional[Any] = None
    approvals_json: Optional[Any] = None
    status: Optional[str] = None
    owner: Optional[str] = None


class TestPlanResponse(TestPlanBase):
    model_config = ConfigDict(from_attributes=True)
    id: str
    project_id: str
    code: str
    created_at: datetime
    updated_at: datetime
    approved_at: Optional[datetime] = None
