"""TodoItem 待辦事項 ORM Model — 首頁日曆 / 側邊欄 widget / 全 backlog 視圖共用。

支援精簡 Backlog 階層:
    Feature(🎯)         產品功能
      ├─ Task(⚙)        開發/測試工作
      ├─ Bug(🐛)        缺陷修復
      └─ Spike(🔬)      技術研究
    (Task / Bug / Spike 也可獨立存在,不一定要掛在 Feature 下)

Story / AC 已從 Backlog 移除,改在 Requirements 模組(parent_id 結構);
要把某個 Backlog 任務跟 Story / AC 綁在一起,請走新的 `todo_links` 連結表。

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
    """統一 7 值狀態 — 對齊 defect / requirement / review。
    舊值 Todo→ASSIGNED, Done→VERIFIED, Cancelled→CLOSED 由 migration 0011 自動轉換。
    """
    NEW = "New"
    ASSIGNED = "Assigned"
    IN_PROGRESS = "InProgress"
    IN_REVIEW = "InReview"
    REWORK_REQUIRED = "ReworkRequired"
    VERIFIED = "Verified"
    CLOSED = "Closed"


class TodoPriority(str, enum.Enum):
    P0 = "P0"
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"


class TodoItemType(str, enum.Enum):
    FEATURE = "Feature"
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
    # 生命週期狀態(配合 entity_versions 的 AB 設計;舊資料 default approved)
    content_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="approved", server_default="approved", index=True,
    )
    # 預定到期日（YYYY-MM-DD），用於日曆 + 過期計算
    due_date: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    status: Mapped[TodoStatus] = mapped_column(
        Enum(TodoStatus, values_callable=lambda x: [e.value for e in x], native_enum=False, length=20), default=TodoStatus.NEW, nullable=False
    )
    priority: Mapped[TodoPriority] = mapped_column(
        Enum(TodoPriority), default=TodoPriority.P2, nullable=False
    )
    # assigned_to 可以是 user(username)或 group(group_id);用 assigned_to_type 區分
    # 'user' → assigned_to 存 users.username;'group' → assigned_to 存 groups.id
    # (Tier D-1 與 Defect/Review/Requirement/TestDocument/TreeNode 對齊命名,
    #  讓 generic /api/assignments router 與 group fan-out 走同一條程式)
    assigned_to: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    assigned_to_type: Mapped[str] = mapped_column(String(10), nullable=False, default="user")
    # 指派時的 audit 欄位:誰在何時指派的
    assigned_by: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    assigned_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    # ── Backlog 階層 ─────────────────────────────────────────────────
    # item_type:Feature / Task / Bug / Spike,預設 Task。
    # parent_id:self-FK,讓 Task/Bug/Spike 掛在 Feature 下;parent 可選空。
    # sprint_label:純文字 sprint 識別,空 = Backlog。
    # native_enum=False:用 varchar + check constraint(避免 PG 端有舊的 todoitemtype
    # ENUM 物件在 INSERT 時做 ::todoitemtype cast 失敗)
    item_type: Mapped[TodoItemType] = mapped_column(
        Enum(TodoItemType, native_enum=False, length=20),
        default=TodoItemType.TASK, nullable=False,
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
