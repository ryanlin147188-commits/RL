"""TestcaseEnvBinding — testcase 綁定的 project env var name 清單。

只記名字,不記 env var id。原因:`project_env_vars` 是「整批替換」管理
(整批 PUT 會掉舊 row id),用 name 連結對 ops 比較直覺,被刪掉的 env
也容易在前端標 "未設定"。
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.auth.tenant import TenantScoped

from .base import Base


class TestcaseEnvBinding(TenantScoped, Base):
    __tablename__ = "testcase_env_bindings"
    __table_args__ = (
        UniqueConstraint(
            "testcase_id",
            "env_var_name",
            name="uq_env_binding_pair",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    testcase_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("tree_nodes.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    env_var_name: Mapped[str] = mapped_column(String(120), nullable=False)
    created_by: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )
