"""WbsLink — WBS Task 跨實體連結(M:N)。

讓 WBS 階層的 ``Task`` 葉節點可以連到外部實體:
  * todo          (任務 — TodoItem)
  * testcase      (測試案例 — TreeNode level=TESTCASE)
  * defect        (缺陷 — Defect)
  * execution_report  (執行紀錄 — ExecutionReport)

Pattern 完全沿用 TodoLink:UniqueConstraint 防 dedupe,target_type 後端白名單驗。
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


# 後端驗證白名單(對齊使用者規格的 4 項:任務 / 測試案例 / 缺陷 / 執行紀錄)
ALLOWED_WBS_TARGET_TYPES = {
    "todo",
    "testcase",
    "defect",
    "execution_report",
}


class WbsLink(Base):
    __tablename__ = "wbs_links"
    __table_args__ = (
        UniqueConstraint(
            "wbs_item_id", "target_type", "target_id",
            name="uq_wbs_link_triple",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4()),
    )
    organization_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    wbs_item_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("wbs_items.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    target_type: Mapped[str] = mapped_column(String(40), nullable=False)
    target_id: Mapped[str] = mapped_column(String(36), nullable=False)

    created_by: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
