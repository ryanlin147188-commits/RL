"""TestVersion 測試版號 ORM Model — 受測對象版本標記(WEB / API / APP)。

設計:
- 一個 project 內可以有多個版號;同 project + 同 platform + 同 version_label 不能重複
- 用作 ExecutionReport / Defect / TestRound 的反向連結,標記「這次跑 / 這個缺陷
  / 這個回合是針對哪個版號」
- 不直接放 SemVer 拆解(major.minor.patch),version_label 自由文字,讓使用者
  可填 "1.2.3" / "v3.5-rc1" / "build-1234" / "2026-Q1" 等不同風格
"""
from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Enum, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class VersionPlatform(str, enum.Enum):
    WEB = "WEB"
    API = "API"
    APP = "APP"


class VersionStatus(str, enum.Enum):
    PLANNED = "planned"        # 未發布,測試計畫中
    RELEASED = "released"      # 已發布,常態測試
    DEPRECATED = "deprecated"  # 已停用,僅查歷史


class TestVersion(Base):
    __tablename__ = "test_versions"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "platform", "version_label",
            name="uq_test_version_project_platform_label",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    organization_id: Mapped[Optional[str]] = mapped_column(
        String(36),
        ForeignKey("organizations.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    project_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    # 用 native_enum=False 存 varchar,避免 PG enum 物件遺留問題(同 todo_item 做法)
    platform: Mapped[VersionPlatform] = mapped_column(
        Enum(VersionPlatform, native_enum=False, length=8),
        nullable=False,
    )
    version_label: Mapped[str] = mapped_column(String(80), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # 發布日期,YYYY-MM-DD 字串(允許空 / 未來日期 = planned)
    released_at: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    status: Mapped[VersionStatus] = mapped_column(
        Enum(VersionStatus, native_enum=False, length=16),
        default=VersionStatus.RELEASED, nullable=False,
    )
    created_by: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )
