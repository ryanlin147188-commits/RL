"""WBS (Work Breakdown Structure) ORM Model。

階層式拆解測試專案的工作項目。每個項目可有 parent，自我參照。
- code 範例：WBS-001、WBS-002…（依專案內編號自動產生；也可儲存階層代號 1.2.3）
- 進度 progress 0-100，狀態 status（NotStarted / InProgress / Completed / Blocked / Cancelled）
- 預計起訖日 + 工時 effort_hours，用來繪製甘特/長條圖
"""
from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Enum, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.auth.tenant import TenantScoped
from .base import Base


class WbsItemType(str, enum.Enum):
    """WBS 階層三層:Feature → WorkPackage → Task。
    Task 為葉節點,可透過 wbs_links 表連到 todo / testcase / defect / execution_report。
    既有資料 default 'Task'(v1 之前所有 row 都當作 Task)。
    """
    FEATURE = "Feature"
    WORK_PACKAGE = "WorkPackage"
    TASK = "Task"


class WbsStatus(str, enum.Enum):
    """統一 7 值狀態 — 對齊 defect / todo / requirement / review。
    舊值 NotStarted→NEW, InProgress→IN_PROGRESS, Completed→VERIFIED,
         Blocked→REWORK_REQUIRED, Cancelled→CLOSED 由 migration 0012 自動轉換。
    為相容舊呼叫方,保留 NOT_STARTED/COMPLETED/BLOCKED/CANCELLED 別名指向新值。
    """
    NEW = "New"
    ASSIGNED = "Assigned"
    IN_PROGRESS = "InProgress"
    IN_REVIEW = "InReview"
    REWORK_REQUIRED = "ReworkRequired"
    VERIFIED = "Verified"
    CLOSED = "Closed"
    NOT_STARTED = NEW
    COMPLETED = VERIFIED
    BLOCKED = REWORK_REQUIRED
    CANCELLED = CLOSED


class WbsItem(TenantScoped, Base):
    __tablename__ = "wbs_items"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    parent_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("wbs_items.id", ondelete="CASCADE"), nullable=True
    )
    code: Mapped[str] = mapped_column(String(60), nullable=False)
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # WBS v1 階層:Feature / WorkPackage / Task。預設 Task(既有資料 + 新建葉節點)。
    item_type: Mapped[WbsItemType] = mapped_column(
        Enum(WbsItemType, values_callable=lambda x: [e.value for e in x], native_enum=False, length=20),
        default=WbsItemType.TASK, server_default=WbsItemType.TASK.value, nullable=False, index=True,
    )
    # 生命週期狀態(配合 entity_versions 的 AB 設計;舊資料 default approved)
    content_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="approved", server_default="approved", index=True,
    )
    status: Mapped[WbsStatus] = mapped_column(
        Enum(WbsStatus, values_callable=lambda x: [e.value for e in x], native_enum=False, length=20),
        default=WbsStatus.NEW, nullable=False,
    )
    progress: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    assignee: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    start_date: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    end_date: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    effort_hours: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )
