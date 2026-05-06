"""測試計畫 (Test Plan) ORM Model。

IEEE 829 風格的結構化計畫文件：scope / strategy / criteria / risks / approvals。
單一專案可有多份 plan（例：UAT Plan、Performance Plan）。
"""
import enum
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Enum, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.auth.tenant import TenantScoped
from .base import Base


class TestPlanStatus(str, enum.Enum):
    """統一 7 值狀態 — 對齊 defect / todo / requirement / review。
    舊值 Draft→NEW, InReview→IN_REVIEW, Approved→VERIFIED, Active→IN_PROGRESS, Closed→CLOSED 由 migration 0012 自動轉換。
    為相容舊呼叫方,保留 DRAFT/APPROVED/ACTIVE 別名指向新值。
    """
    NEW = "New"
    ASSIGNED = "Assigned"
    IN_PROGRESS = "InProgress"
    IN_REVIEW = "InReview"
    REWORK_REQUIRED = "ReworkRequired"
    VERIFIED = "Verified"
    CLOSED = "Closed"
    # 別名(指向新值)— 舊程式 TestPlanStatus.DRAFT / APPROVED / ACTIVE 仍有效
    DRAFT = NEW
    APPROVED = VERIFIED
    ACTIVE = IN_PROGRESS


class TestPlan(TenantScoped, Base):
    __tablename__ = "test_plans"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    code: Mapped[str] = mapped_column(String(40), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    version: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    # Markdown 文字
    scope_in_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    scope_out_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    test_strategy_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    resources_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    schedule_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    risks_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # 結構化條件 / 簽核紀錄
    entry_criteria_json: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    exit_criteria_json: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    approvals_json: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    status: Mapped[TestPlanStatus] = mapped_column(
        Enum(TestPlanStatus, values_callable=lambda x: [e.value for e in x], native_enum=False, length=20), default=TestPlanStatus.NEW, nullable=False
    )
    owner: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )
    approved_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
