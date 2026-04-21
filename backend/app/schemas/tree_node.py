from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict

from app.models.tree_node import LevelType


class TreeNodeCreate(BaseModel):
    project_id: str
    parent_id: Optional[str] = None
    name: str
    sort_order: int = 0


class TreeNodeUpdate(BaseModel):
    name: str


class TreeNodePartialUpdate(BaseModel):
    """PATCH 語意：所有欄位均可選。"""
    name: Optional[str] = None
    sort_order: Optional[int] = None


class TreeNodeResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    project_id: str
    parent_id: Optional[str]
    level_type: LevelType
    name: str
    sort_order: int
    children: list[TreeNodeResponse] = []
