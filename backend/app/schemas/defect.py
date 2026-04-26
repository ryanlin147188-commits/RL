"""Defect 缺陷 Pydantic Schemas。"""
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict


class DefectBase(BaseModel):
    title: str
    description: Optional[str] = None
    steps_to_reproduce: Optional[str] = None
    expected_result: Optional[str] = None
    actual_result: Optional[str] = None
    severity: str = "Minor"
    priority: str = "P2"
    status: str = "New"
    reporter: Optional[str] = None
    assignee: Optional[str] = None
    linked_testcase_id: Optional[str] = None
    linked_report_id: Optional[str] = None
    external_url: Optional[str] = None


class DefectCreate(DefectBase):
    project_id: str
    code: Optional[str] = None  # 留空 → 自動產 BUG-NNN


class DefectUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    steps_to_reproduce: Optional[str] = None
    expected_result: Optional[str] = None
    actual_result: Optional[str] = None
    severity: Optional[str] = None
    priority: Optional[str] = None
    status: Optional[str] = None
    assignee: Optional[str] = None
    linked_testcase_id: Optional[str] = None
    linked_report_id: Optional[str] = None
    external_url: Optional[str] = None


class DefectResponse(DefectBase):
    model_config = ConfigDict(from_attributes=True)
    id: str
    project_id: str
    code: str
    created_at: datetime
    updated_at: datetime
    closed_at: Optional[datetime] = None
