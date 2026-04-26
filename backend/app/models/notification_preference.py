"""NotificationPreference 通知設定 ORM Model — 每個使用者一筆，存事件 → 通道映射。"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import JSON, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class NotificationPreference(Base):
    __tablename__ = "notification_preferences"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    # username 不一定對應到後端使用者表（auth 暫時是 client-side 的 localStorage），
    # 用字串就好；NULL = 全域預設
    username: Mapped[Optional[str]] = mapped_column(String(100), nullable=True, unique=True)
    # events_json：{ event_key: { in_app: bool, email: bool } }
    # 例：{"defect.created": {"in_app":true, "email":true}, "run.failed": {...}}
    events_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )
