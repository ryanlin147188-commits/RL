"""OrgInvite 邀請碼 ORM Model — 自助註冊用。

設計:
- 單次使用(一個 token 只能 redeem 一次);批量邀請就建多筆。
- 可指定 `email`(限該 email 才能用)或留空(任何 email 皆可用)。
- 可帶起始 `role_id` / `group_id`,註冊成功後自動套用。
- `expires_at` 過期就無效;預設 7 天(由 router 控制)。
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class OrgInvite(Base):
    __tablename__ = "org_invites"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    token: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    organization_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    # 限定特定 email(可空 = 開放任何 email)
    email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    role_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("roles.id", ondelete="SET NULL"), nullable=True,
    )
    group_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("groups.id", ondelete="SET NULL"), nullable=True,
    )
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    # redeem 紀錄(單次使用:used_at 不為空 = 已用)
    used_by: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    used_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_by: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
