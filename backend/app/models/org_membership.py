"""OrgMembership — 一個使用者跟一個組織的關係(N:M),取代 User.organization_id 1:1 FK。

每個 row 代表「使用者 U 屬於組織 O,在這個組織裡擔任 R 角色」。
與既有 ``User.organization_id`` 並存:
* 該欄位仍存在,當「目前 active 的組織」(JWT 內 ``org_id`` 來源、所有 query 過濾鍵)。
* OrgMembership 才是「使用者實際被授權能進入的組織清單」。
* 切換 active 組織 = 把 ``users.organization_id`` 更新成另一個有 OrgMembership 的 org_id。

是否 cascade 刪除:
* org 刪除 → 該 org 下的所有 OrgMembership cascade 刪除。
* user 刪除 → 該 user 的所有 OrgMembership cascade 刪除(同 GroupMembership 模式)。
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class OrgMembership(Base):
    __tablename__ = "org_memberships"
    __table_args__ = (
        UniqueConstraint("username", "organization_id", name="uq_org_memberships_user_org"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    username: Mapped[str] = mapped_column(
        String(80),
        ForeignKey("users.username", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # v1.1.7 Phase 3 shadow column。Phase 7 PK cutover 之前 application 仍
    # 主要讀寫 username,只有 fastapi-users 內部走 user_id。
    user_id: Mapped[Optional[str]] = mapped_column(
        String(36), nullable=True, index=True,
    )
    organization_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("roles.id", ondelete="SET NULL"), nullable=True,
    )
    # 該使用者多個 OrgMembership 中,登入時要切到哪一個 active org。每使用者最多一個 row 為 True。
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # active / invited(尚未接受邀請)/ disabled(被停權但保留歷史紀錄)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    joined_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    invited_by: Mapped[Optional[str]] = mapped_column(
        String(80),
        ForeignKey("users.username", ondelete="SET NULL"),
        nullable=True,
    )
    invited_by_user_id: Mapped[Optional[str]] = mapped_column(
        String(36), nullable=True,
    )
