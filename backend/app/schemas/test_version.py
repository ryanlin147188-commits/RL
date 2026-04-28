"""TestVersion Pydantic schemas。"""
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict


class TestVersionBase(BaseModel):
    project_id: str
    platform: str  # WEB / API / APP
    version_label: str
    description: Optional[str] = None
    released_at: Optional[str] = None
    status: str = "released"  # planned / released / deprecated


class TestVersionCreate(TestVersionBase):
    pass


class TestVersionUpdate(BaseModel):
    platform: Optional[str] = None
    version_label: Optional[str] = None
    description: Optional[str] = None
    released_at: Optional[str] = None
    status: Optional[str] = None


class TestVersionResponse(TestVersionBase):
    model_config = ConfigDict(from_attributes=True)
    id: str
    organization_id: Optional[str] = None
    created_by: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    # 動態欄位:有多少 reports / defects / rounds 引用此版號
    usage_count: int = 0
