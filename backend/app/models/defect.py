"""缺陷管理 (Defect Management) ORM Model。

每個缺陷可以：
- 連結到一個測試案例（linked_testcase_id → tree_nodes）
- 連結到一份執行報告（linked_report_id → execution_reports）
- 上傳多張截圖／附件（attachments_json 為 [{name, url, size, type}, ...] 的列表）

狀態機（簡化版）：
  New → Assigned → InProgress → Fixed → Verified → Closed
                                                ↓
                                           Reopened（回 Assigned）
"""
import enum
import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import JSON, DateTime, Enum, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class DefectSeverity(str, enum.Enum):
    CRITICAL = "Critical"
    MAJOR = "Major"
    MINOR = "Minor"
    TRIVIAL = "Trivial"


class DefectPriority(str, enum.Enum):
    P0 = "P0"
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"


class DefectStatus(str, enum.Enum):
    NEW = "New"
    ASSIGNED = "Assigned"
    IN_PROGRESS = "InProgress"
    FIXED = "Fixed"
    VERIFIED = "Verified"
    CLOSED = "Closed"
    REOPENED = "Reopened"
    WONT_FIX = "WontFix"


class Defect(Base):
    __tablename__ = "defects"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    code: Mapped[str] = mapped_column(String(40), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    steps_to_reproduce: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    expected_result: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    actual_result: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    severity: Mapped[DefectSeverity] = mapped_column(
        Enum(DefectSeverity), default=DefectSeverity.MINOR, nullable=False
    )
    priority: Mapped[DefectPriority] = mapped_column(
        Enum(DefectPriority), default=DefectPriority.P2, nullable=False
    )
    status: Mapped[DefectStatus] = mapped_column(
        Enum(DefectStatus), default=DefectStatus.NEW, nullable=False
    )
    reporter: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    assignee: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    linked_testcase_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("tree_nodes.id", ondelete="SET NULL"), nullable=True
    )
    linked_report_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    # 反向關聯到 TestVersion(可空;若版號被刪 → set null)
    test_version_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("test_versions.id", ondelete="SET NULL"), nullable=True, index=True,
    )
    attachments_json: Mapped[Optional[list[dict[str, Any]]]] = mapped_column(
        JSON, nullable=True, default=list
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
