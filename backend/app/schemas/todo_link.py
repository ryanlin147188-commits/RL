"""TodoLink Pydantic schemas — Backlog 跨實體連結。"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict


class TodoLinkBase(BaseModel):
    target_type: str  # 後端 router 校驗白名單
    target_id: str
    link_kind: str = "relates_to"
    note: Optional[str] = None


class TodoLinkCreate(TodoLinkBase):
    pass


class TodoLinkResponse(TodoLinkBase):
    model_config = ConfigDict(from_attributes=True)
    id: str
    todo_id: str
    organization_id: Optional[str] = None
    created_at: datetime
    created_by: Optional[str] = None
    # 為前端顯示方便,序列化時可填(由 router 用 JOIN 補上)
    target_title: Optional[str] = None
    target_code: Optional[str] = None


class TodoSummaryForLink(BaseModel):
    """反向查詢:一個 target 被哪些 Todo 連結。回傳簡略 summary 給徽章 / popover。"""
    id: str
    title: str
    item_type: str
    status: str
    priority: str
    assignee: Optional[str] = None
    link_kind: str = "relates_to"
