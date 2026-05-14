"""TodoLink — Backlog 跨實體連結(M:N)。

讓任何 TodoItem(Feature / Task / Bug / Spike)可以連到多個外部實體(需求 / 案例 /
缺陷 / 測試計畫 / 測試回合 / 里程碑 / WBS / 測試文件 / Project),用於 RTM 追溯鏈、
測試看板、需求清單上下文展示。

取代舊有的 `todo_items.related_entity_type` / `related_entity_id` 兩個欄位:
舊欄位 N:1 不夠用(一個 Story 常會牽涉多個 TestCase + Defect),且前端從未讀過。
新表 `todo_links` 提供 N:M;舊欄位保留為唯讀直到清理 PR。
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


# 後端驗證白名單;router 校驗 target_type 必須 in 此集合
ALLOWED_TARGET_TYPES = {
    "requirement",
    "testcase",
    "defect",
    "test_plan",
    "test_round",
    "test_milestone",
    "wbs",
    "project",
}


class TodoLink(Base):
    __tablename__ = "todo_links"
    __table_args__ = (
        UniqueConstraint(
            "todo_id", "target_type", "target_id", "link_kind",
            name="uq_todo_link_quad",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    organization_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    todo_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("todo_items.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    # 白名單見 ALLOWED_TARGET_TYPES
    target_type: Mapped[str] = mapped_column(String(40), nullable=False)
    target_id: Mapped[str] = mapped_column(String(36), nullable=False)
    # v1 自由字串(預設 relates_to);後續可限定 verifies / blocks / duplicates 等
    link_kind: Mapped[str] = mapped_column(String(40), nullable=False, default="relates_to")
    note: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    created_by: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
