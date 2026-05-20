"""ProjectInvite — pull-based 加入專案的邀請紀錄。

新流程取代直接 INSERT ProjectMember:
1. admin 對 email X 發邀請 → 建 ProjectInvite + 寄信給 X
2. X (用 Zoho / 密碼登入後) 在 #usersettings 或 ?invite_code=XXX 兌換
3. 兌換時驗證 (a) code 未過期 (b) X 的登入 email 等於 invitee_email
4. 通過 → 建 ProjectMember + 標記 redeemed

設計要點:
* invite_code 是 URL-safe random 24 char,unique;放 URL query 不會被路由器 log 截斷。
* status: pending / redeemed / expired / revoked。expired 是 lazy migration
  (沒人定時 sweep,redeem 時看 expires_at)。
* invitee_email 全部 lower,比對也 lower(case-insensitive)。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, String, Index
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


def _gen_invite_code() -> str:
    """24-char URL-safe code,~144 bit entropy。"""
    import secrets
    return secrets.token_urlsafe(18)[:24]


class ProjectInvite(Base):
    __tablename__ = "project_invites"
    __table_args__ = (
        Index("ix_project_invites_code", "invite_code", unique=True),
        Index("ix_project_invites_email", "invitee_email"),
        Index("ix_project_invites_proj", "project_id"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    organization_id: Mapped[str] = mapped_column(String(36), nullable=False)
    invitee_email: Mapped[str] = mapped_column(String(255), nullable=False)
    # 該專案內角色 override;NULL = 沿用使用者 OrgMembership 角色。
    role_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("roles.id", ondelete="SET NULL"), nullable=True
    )
    invite_code: Mapped[str] = mapped_column(
        String(32), nullable=False, unique=True, default=_gen_invite_code
    )
    inviter_username: Mapped[str] = mapped_column(String(64), nullable=False)
    # pending / redeemed / expired / revoked
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    redeemed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    redeemed_by_username: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True
    )

    @staticmethod
    def default_expires_at(days: int = 7) -> datetime:
        return datetime.utcnow() + timedelta(days=days)
