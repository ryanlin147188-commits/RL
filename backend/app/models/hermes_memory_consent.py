"""Hermes / mem0 memory consent ORM。

PR3 範圍:per-user 開關,控制 mem0 fact extraction 是否啟用。

設計:
- 主鍵用 username(對齊既有 hermes_session_refs.owner 慣例)
- extraction_enabled 預設 True(opt-out 設計;UI 上會明示「啟用 = 額外消耗 token quota」)
- paused_session_ids 是 JSON dict {session_id: paused_until_ts (epoch sec)},
  用 cron 自然清理過期項;send_message 前讀,過期就忽略
- 沒 row 等於 enabled=True、paused 空(`_get_or_default_consent` 處理)
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class HermesMemoryConsent(Base):
    __tablename__ = "hermes_memory_consents"

    username: Mapped[str] = mapped_column(String(100), primary_key=True)
    organization_id: Mapped[Optional[str]] = mapped_column(
        String(36),
        ForeignKey("organizations.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    extraction_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True,
    )
    # {session_id (str): paused_until_epoch_sec (float)};過期項 send_message 時忽略
    paused_session_ids: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow,
    )
