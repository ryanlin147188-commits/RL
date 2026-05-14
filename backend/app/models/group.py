"""Group(團隊群組)+ GroupMembership ORM Model。

設計重點:
- 群組可巢狀(parent_id self-FK);通知 fan-out 時遞迴展開子群組成員。
- 群組可以被當成 todo.assigned_to(assigned_to_type='group',assigned_to=group_id)。
- 同一 organization_id 內 name 唯一;不同 org 內可重名。
- group_type 是純文字 label('team' / 'squad' / 'dept' / 'project'),沒列舉硬約束,
  之後想加新分類不用 migration。
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class Group(Base):
    __tablename__ = "groups"
    __table_args__ = (
        UniqueConstraint("organization_id", "name", name="uq_groups_org_name"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    organization_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True, index=True,
    )
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    group_type: Mapped[str] = mapped_column(String(20), nullable=False, default="team")
    # 巢狀 parent FK;ondelete=SET NULL 讓父群組刪除時子群組升頂(不連帶刪)
    parent_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("groups.id", ondelete="SET NULL"), nullable=True, index=True,
    )
    created_by: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class GroupMembership(Base):
    __tablename__ = "group_memberships"

    group_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("groups.id", ondelete="CASCADE"),
        primary_key=True,
    )
    username: Mapped[str] = mapped_column(
        String(80),
        ForeignKey("users.username", ondelete="CASCADE"),
        primary_key=True,
    )
    # v1.1.7 Phase 3 shadow column。Phase 7 換 PK 時 username 退場,user_id
    # 跟 group_id 一起當 composite PK。
    user_id: Mapped[Optional[str]] = mapped_column(
        String(36), nullable=True, index=True,
    )
    # owner / admin / member;owner 至少要一人,UI 上負責防呆
    role_in_group: Mapped[str] = mapped_column(String(20), nullable=False, default="member")
    joined_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
