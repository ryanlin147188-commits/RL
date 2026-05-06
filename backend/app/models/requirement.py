"""需求 (Requirement) + RTM 關聯表 ORM Model。

需求 ↔ 測試案例：多對多關聯（一個需求可能由多個案例驗證；一個案例也可能涵蓋多個需求）。
"""
import enum
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Enum, ForeignKey, PrimaryKeyConstraint, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.auth.tenant import Assignable, TenantScoped
from .base import Base


class RequirementSource(str, enum.Enum):
    PRD = "PRD"                 # 產品需求
    CUSTOMER = "Customer"       # 客戶來源
    REGULATORY = "Regulatory"   # 法規 / 合規
    SECURITY = "Security"       # 資安
    INTERNAL = "Internal"       # 內部


class RequirementPriority(str, enum.Enum):
    MUST = "Must"
    SHOULD = "Should"
    COULD = "Could"
    WONT = "Wont"


class RequirementStatus(str, enum.Enum):
    """統一 7 值狀態 — 對齊 defect / todo / review。
    舊值 Draft→NEW, Approved→ASSIGNED, Implemented→IN_REVIEW, Deprecated→CLOSED 由 migration 0011 轉換。
    """
    NEW = "New"
    ASSIGNED = "Assigned"
    IN_PROGRESS = "InProgress"
    IN_REVIEW = "InReview"
    REWORK_REQUIRED = "ReworkRequired"
    VERIFIED = "Verified"
    CLOSED = "Closed"


class Requirement(Assignable, TenantScoped, Base):
    __tablename__ = "requirements"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    code: Mapped[str] = mapped_column(String(40), nullable=False)
    parent_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("requirements.id", ondelete="SET NULL"), nullable=True
    )
    # 生命週期狀態(配合 entity_versions 的 AB 設計;舊資料 default approved)
    content_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="approved", server_default="approved", index=True,
    )
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    source: Mapped[RequirementSource] = mapped_column(
        Enum(RequirementSource), default=RequirementSource.PRD, nullable=False
    )
    priority: Mapped[RequirementPriority] = mapped_column(
        Enum(RequirementPriority), default=RequirementPriority.SHOULD, nullable=False
    )
    status: Mapped[RequirementStatus] = mapped_column(
        Enum(RequirementStatus), default=RequirementStatus.NEW, nullable=False
    )
    owner: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class RequirementTestcaseLink(TenantScoped, Base):
    """RTM 多對多關聯：requirement ↔ testcase（tree_nodes.id where level_type='TESTCASE'）"""
    __tablename__ = "requirement_testcase_links"

    requirement_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("requirements.id", ondelete="CASCADE"), nullable=False
    )
    testcase_node_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tree_nodes.id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        PrimaryKeyConstraint("requirement_id", "testcase_node_id"),
    )
