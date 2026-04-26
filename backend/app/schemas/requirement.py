"""Requirement 需求 + RTM 關聯 Pydantic Schemas。"""
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict


class RequirementBase(BaseModel):
    title: str
    description: Optional[str] = None
    parent_id: Optional[str] = None
    source: str = "PRD"
    priority: str = "Should"
    status: str = "Draft"
    owner: Optional[str] = None


class RequirementCreate(RequirementBase):
    project_id: str
    code: Optional[str] = None  # 留空 → 自動產 REQ-NNN


class RequirementUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    parent_id: Optional[str] = None
    source: Optional[str] = None
    priority: Optional[str] = None
    status: Optional[str] = None
    owner: Optional[str] = None


class RequirementResponse(RequirementBase):
    model_config = ConfigDict(from_attributes=True)
    id: str
    project_id: str
    code: str
    created_at: datetime
    updated_at: datetime
    # 額外欄位（運算後加）：對應的測試案例 id 清單
    linked_testcase_ids: list[str] = []


class RtmLinkUpdate(BaseModel):
    """整批替換某個需求的關聯測試案例。"""
    testcase_node_ids: list[str]


class RtmCell(BaseModel):
    """RTM 矩陣單一格：requirement × testcase 的目前狀態。"""
    requirement_id: str
    testcase_node_id: str
    last_status: Optional[str] = None  # PASSED / FAILED / RUNNING / None


class RtmMatrixResponse(BaseModel):
    """整張 RTM 矩陣資料。"""
    requirements: list[RequirementResponse]
    testcases: list[dict]      # [{id, title, platform, page}, ...]
    cells: list[RtmCell]       # 每對有關聯的 (req, tc) 配上最新狀態
