"""Test Data Set (DDT) Pydantic Schemas。"""
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict


class TestDataSetBase(BaseModel):
    name: str
    description: Optional[str] = None
    category: str = "Other"
    columns_json: list[str] = []
    rows_json: list[dict[str, Any]] = []
    linked_testcase_ids: Optional[list[str]] = None
    owner: Optional[str] = None


class TestDataSetCreate(TestDataSetBase):
    project_id: str
    code: Optional[str] = None  # 留空 → 自動產 DS-NNN


class TestDataSetUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    category: Optional[str] = None
    columns_json: Optional[list[str]] = None
    rows_json: Optional[list[dict[str, Any]]] = None
    linked_testcase_ids: Optional[list[str]] = None
    owner: Optional[str] = None


class TestDataSetResponse(TestDataSetBase):
    model_config = ConfigDict(from_attributes=True)
    id: str
    project_id: str
    code: str
    created_at: datetime
    updated_at: datetime
