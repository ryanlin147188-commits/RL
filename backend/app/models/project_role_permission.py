"""ProjectRolePermission — 同一個 Role 在特定 project 內的 permission override。

v1.1.6:讓「Project-Tester 在 project A 可以 write,在 project B 只能 read」
這種精修需求不必為每個專案複製整套 Role,直接 override 該 (project, role)
組合的 permission 清單即可。

沒在這張表的 (project, role) 組合 → 該專案內該 role 沿用 ``roles.permissions_json``
全域預設(Casbin sync 寫一條 ``p, <role>, <pid_dom>, ..., ...`` 即可);
表內有 row → Casbin sync 改用 ``<role>@<short_pid>`` alias 寫 override 的
permission set。
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class ProjectRolePermission(Base):
    __tablename__ = "project_role_permissions"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "role_id", name="uq_project_role_permissions_pair",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4()),
    )
    project_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("roles.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    permissions_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow,
    )
