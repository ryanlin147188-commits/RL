from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict


class ProjectCreate(BaseModel):
    """建立測試專案 — name 必填，其餘為選填豐富欄位。"""
    name: str
    description: Optional[str] = None
    owner: Optional[str] = None
    status: Optional[str] = None
    start_date: Optional[str] = None
    target_date: Optional[str] = None
    tags: Optional[str] = None


class ProjectUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    owner: Optional[str] = None
    status: Optional[str] = None
    start_date: Optional[str] = None
    target_date: Optional[str] = None
    tags: Optional[str] = None


class ProjectResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    description: Optional[str] = None
    owner: Optional[str] = None
    status: Optional[str] = None
    start_date: Optional[str] = None
    target_date: Optional[str] = None
    tags: Optional[str] = None
    created_at: datetime
    updated_at: datetime
