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


class WbsStatus(str, enum.Enum):
    NOT_STARTED = "NotStarted"
    IN_PROGRESS = "InProgress"
    COMPLETED = "Completed"
    BLOCKED = "Blocked"
    CANCELLED = "Cancelled"


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
    # 生命週期狀態(配合 entity_versions 的 AB 設計;舊資料 default approved)
    content_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="approved", server_default="approved", index=True,
    )
    status: Mapped[WbsStatus] = mapped_column(
        Enum(WbsStatus), default=WbsStatus.NOT_STARTED, nullable=False
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
