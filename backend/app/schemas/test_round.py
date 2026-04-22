"""測試回合（Test Round）Pydantic schemas。"""
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class TestRoundBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    node_ids: list[str] = Field(..., min_length=1)
    description: Optional[str] = None
    execution_mode: str = "docker"


class TestRoundCreate(TestRoundBase):
    project_id: str


class TestRoundUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=200)
    node_ids: Optional[list[str]] = None
    description: Optional[str] = None
    execution_mode: Optional[str] = None


class TestRoundResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    project_id: str
    node_ids: list[str] = []
    node_titles: list[str] = []
    description: Optional[str] = None
    execution_mode: str = "docker"
    # 最近一次執行結果（由 router 動態填入）
    last_run_at: Optional[datetime] = None
    last_report_ids: list[str] = []
    created_at: datetime
    updated_at: datetime
