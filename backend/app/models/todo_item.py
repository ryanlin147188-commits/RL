"""TodoItem 待辦事項 ORM Model — 首頁日曆 / 側邊欄 widget / 全 backlog 視圖共用。

支援 Agile/Scrum 階層:
    Epic(🎯)
      └─ Story(📖)
           ├─ Task(⚙)     開發/測試工作
           ├─ Bug(🐛)     缺陷修復
           └─ Spike(🔬)   技術研究
    (Bug / Spike 也可獨立存在,不一定要掛在 Story 下)

`sprint_label` 為純文字 label(例「Sprint 23」/「2026-W18」),空值代表 Backlog。
不另開 sprints 表 — 字串夠輕,Sprint 後續若要管期間/狀態可獨立做。
"""
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


class TodoItemType(str, enum.Enum):
    EPIC = "Epic"
    STORY = "Story"
    TASK = "Task"
    BUG = "Bug"
    SPIKE = "Spike"


class TodoItem(Base):
    __tablename__ = "todo_items"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    organization_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True, index=True,
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
    # ── Backlog 階層 ─────────────────────────────────────────────────
    # item_type:Epic / Story / Task / Bug / Spike,預設 Task。
    # parent_id:self-FK,讓 Task/Bug/Spike 掛在 Story 下、Story 掛在 Epic 下。
    # sprint_label:純文字 sprint 識別,空 = Backlog。
    item_type: Mapped[TodoItemType] = mapped_column(
        Enum(TodoItemType), default=TodoItemType.TASK, nullable=False
    )
    parent_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("todo_items.id", ondelete="SET NULL"), nullable=True, index=True,
    )
    sprint_label: Mapped[Optional[str]] = mapped_column(String(80), nullable=True, index=True)
    # 關聯實體：方便連到缺陷 / 案例 / 計畫等（type+id），UI 上可作為跳轉連結
    related_entity_type: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    related_entity_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )
