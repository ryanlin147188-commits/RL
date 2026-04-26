"""TodoItem 待辦事項 ORM Model — 首頁日曆與待辦清單使用。"""
from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Enum, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class TodoStatus(str, enum.Enum):
    TODO = "Todo"
    IN_PROGRESS = "InProgress"
    DONE = "Done"
    CANCELLED = "Cancelled"


class TodoPriority(str, enum.Enum):
    P0 = "P0"
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"


class TodoItem(Base):
    __tablename__ = "todo_items"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    # 專案關聯：可空 → 跨專案 / 個人 todo
    project_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=True
    )
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # 預定到期日（YYYY-MM-DD），用於日曆 + 過期計算
    due_date: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    status: Mapped[TodoStatus] = mapped_column(
        Enum(TodoStatus), default=TodoStatus.TODO, nullable=False
    )
    priority: Mapped[TodoPriority] = mapped_column(
        Enum(TodoPriority), default=TodoPriority.P2, nullable=False
    )
    assignee: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    # 關聯實體：方便連到缺陷 / 案例 / 計畫等（type+id），UI 上可作為跳轉連結
    related_entity_type: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    related_entity_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )
