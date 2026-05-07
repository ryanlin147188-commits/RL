"""TestcasePreconditionLink — testcase 之間的「前置案例」M:N。

設計重點:
  * 單向 FK:`testcase_id` 是主案例,`precondition_testcase_id` 是會在主案例
    執行前先跑一次的前置案例;不允許自我參考(由 service 層拒絕)。
  * `sort_order`:同一個 testcase 上多個前置時的執行順序;以整數遞增。
  * `enabled`:暫時停用某條前置而不刪 row。
  * `on_failure='stop'`(目前唯一支援值):前置失敗就中斷主案例。預留欄位
    給未來 'continue' / 'skip' 等行為。
  * UniqueConstraint(testcase_id, precondition_testcase_id):同一對只能掛一次。
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.auth.tenant import TenantScoped

from .base import Base


class TestcasePreconditionLink(TenantScoped, Base):
    __tablename__ = "testcase_precondition_links"
    __table_args__ = (
        UniqueConstraint(
            "testcase_id",
            "precondition_testcase_id",
            name="uq_precondition_pair",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    testcase_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("tree_nodes.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    precondition_testcase_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("tree_nodes.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # 預留多種失敗行為;v1 只實作 'stop'
    on_failure: Mapped[str] = mapped_column(String(20), nullable=False, default="stop")
    created_by: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )
