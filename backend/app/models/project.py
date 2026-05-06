"""Project（測試專案）ORM Model。

除了原本的 id / name 之外，新增以下選填欄位以支援「測試專案」工作區：
- description: 專案簡介
- owner: 負責人
- status: 統一 7 值狀態（New / Assigned / InProgress / InReview / ReworkRequired / Verified / Closed）
         遷移後對應:Planning→New, Active→InProgress, OnHold→Assigned, Archived→Closed
- start_date / target_date: 預計起訖日（YYYY-MM-DD 字串，方便前端 input[type=date]）
- tags: 用「,」分隔的標籤字串

所有新欄位皆為 nullable，舊資料由 migration 0012 自動轉換。
"""
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    organization_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True, index=True,
    )
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    owner: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    status: Mapped[Optional[str]] = mapped_column(String(40), nullable=True, default="InProgress")
    start_date: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    target_date: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    tags: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    # Relationships
    tree_nodes: Mapped[list["TreeNode"]] = relationship(
        "TreeNode",
        back_populates="project",
        cascade="all, delete-orphan",
    )
    execution_reports: Mapped[list["ExecutionReport"]] = relationship(
        "ExecutionReport",
        back_populates="project",
        cascade="all, delete-orphan",
    )
