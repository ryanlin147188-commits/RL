"""Notification 站內通知 ORM Model — 使用者收件匣（top bar 鈴鐺所讀取的訊息來源）。

設計重點：
- 收件人欄位用 `recipient`(username)，與 NotificationPreference 一致；
  避免硬綁 user FK，允許「未來新增的使用者」也能被通知（系統廣播）。
- `event_key` 對應 NotificationPreference.events_json 的 key，用來判斷該事件
  是否需要寄 in-app / email；router 內留著欄位但本檔不做投遞邏輯。
- `link` 為前端 SPA 內的 hash route 或外部 URL，使用者點擊通知可跳轉。
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    organization_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    recipient: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    event_key: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    # info / success / warning / error
    level: Mapped[str] = mapped_column(String(20), nullable=False, default="info")
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    body: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    link: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    related_entity_type: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    related_entity_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    is_read: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    read_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False, index=True,
    )

    __table_args__ = (
        Index("ix_notifications_recipient_unread", "recipient", "is_read"),
    )
