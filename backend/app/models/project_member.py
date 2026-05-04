"""ProjectMember — 一個使用者跟一個專案的關係(N:M)+ 該專案內的角色。

每個 row 代表「使用者 U 是專案 P 的成員,在這個專案裡擔任 R 角色」。
取代「同 organization 的所有使用者自動看得到所有專案」的隱性規則。

權限解析(``require_project_permission``):
* 必要條件:current_user 在 active org 有 ``OrgMembership``。
* 進一步:在該 project 有 ``ProjectMember`` 才能讀寫該專案。
* 角色解析:``ProjectMember.role_id`` 優先,若 NULL 退回 ``OrgMembership.role_id``。
* superuser 全 bypass。

是否 cascade 刪除:
* project 刪除 → 該 project 下的所有 ProjectMember cascade 刪除。
* user 刪除 → 該 user 的所有 ProjectMember cascade 刪除。
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class ProjectMember(Base):
    __tablename__ = "project_members"
    __table_args__ = (
        UniqueConstraint("project_id", "username", name="uq_project_members_project_user"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    project_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    username: Mapped[str] = mapped_column(
        String(80),
        ForeignKey("users.username", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # NULL = 從 OrgMembership.role_id 繼承;非 NULL = 該專案 override 的角色
    role_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("roles.id", ondelete="SET NULL"), nullable=True,
    )
    # active / invited / disabled
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    joined_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    invited_by: Mapped[Optional[str]] = mapped_column(
        String(80),
        ForeignKey("users.username", ondelete="SET NULL"),
        nullable=True,
    )
