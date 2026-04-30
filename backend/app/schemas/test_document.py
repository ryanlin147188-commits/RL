"""Test Document Pydantic Schemas。"""
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict


class TestDocumentBase(BaseModel):
    title: str
    category: str = "Note"
    content_md: Optional[str] = None
    summary: Optional[str] = None
    owner: Optional[str] = None
    tags: Optional[str] = None
    # Phase 2 — generic assignment fields. Populated by /api/assignments;
    # nullable when nobody has been assigned via the generic endpoint.
    assigned_to: Optional[str] = None
    assigned_to_type: Optional[str] = None
    assigned_by: Optional[str] = None
    assigned_at: Optional[datetime] = None


class TestDocumentCreate(TestDocumentBase):
    project_id: str
    code: Optional[str] = None  # 留空 → 自動產 DOC-NNN


class TestDocumentUpdate(BaseModel):
    title: Optional[str] = None
    category: Optional[str] = None
    content_md: Optional[str] = None
    summary: Optional[str] = None
    owner: Optional[str] = None
    tags: Optional[str] = None


class TestDocumentResponse(TestDocumentBase):
    model_config = ConfigDict(from_attributes=True)
    id: str
    project_id: str
    code: str
    created_at: datetime
    updated_at: datetime
