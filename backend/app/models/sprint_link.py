"""SprintLink — Sprint(test_schedules)跨實體連結(M:N)。

每個 Sprint(即「測試時程」TestSchedule)可以連到多個目標實體:測試案例 / 測試報告
/ 缺陷 / TestRun / 看板任務(TodoItem)。完全沿用 TodoLink pattern,只是 owner 從
todo_id 改成 schedule_id。

舊有 `test_schedules.linked_target_type` / `linked_target_id` 兩個欄位是單一連結,
保留為唯讀(讀取時前端會 stitch 進 GET /links 結果並標記為 legacy)。新建/修改一律
走這個新表。
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


# router 會校驗 target_type 必須 in 此集合。
# 加 `todo`(看板任務 = TodoItem)— 跟 TodoLink 不同,TodoLink 是「Todo 連到別人」,
# SprintLink 是「Sprint 連到 Todo / Testcase / Report / Defect / TestRun」。
ALLOWED_TARGET_TYPES = {
    "testcase",
    "test_round",
    "report",
    "defect",
    "todo",
}


class SprintLink(Base):
    __tablename__ = "sprint_links"
    __table_args__ = (
        UniqueConstraint(
            "schedule_id", "target_type", "target_id", "link_kind",
            name="uq_sprint_link_quad",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    organization_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    schedule_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("test_schedules.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    target_type: Mapped[str] = mapped_column(String(40), nullable=False)
    target_id: Mapped[str] = mapped_column(String(36), nullable=False)
    link_kind: Mapped[str] = mapped_column(String(40), nullable=False, default="relates_to")
    note: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    created_by: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
