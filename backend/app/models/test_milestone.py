"""測試時程 (Test Milestone / Schedule) ORM Model。

代表一個有起訖日期的測試里程碑（如：Sprint 5 UAT、Release 2.0 回歸測試窗）。
與既有 schedules（cron 排程）不同，這是「日曆 / Gantt」視角的時程規劃。
"""
import enum
import uuid
from datetime import date, datetime
from typing import Optional

from sqlalchemy import Date, DateTime, Enum, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class MilestoneStatus(str, enum.Enum):
    PLANNED = "Planned"
    IN_PROGRESS = "InProgress"
    COMPLETED = "Completed"
    CANCELLED = "Cancelled"


class TestMilestone(Base):
    __tablename__ = "test_milestones"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[MilestoneStatus] = mapped_column(
        Enum(MilestoneStatus), default=MilestoneStatus.PLANNED, nullable=False
    )
    owner: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    # 顏色（calendar / gantt 用）；HEX 字串例如 "#3b82f6"
    color: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    # 連結到測試回合（執行階段）
    linked_test_round_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    # 連結到測試計畫
    linked_test_plan_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )
