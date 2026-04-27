"""Mock endpoint persistence — 把原本只存於前端 localStorage 的 Mock 設定改存 DB。

每個 Mock endpoint 為 (method, path) 的回應定義 + 試打範本;支援 Faker
佔位符(`{{name}}` / `{{uuid}}` / `{{int:1,100}}`)在 headers / body 內展開。
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class MockEndpoint(Base):
    __tablename__ = "mock_endpoints"
    __table_args__ = (
        UniqueConstraint("project_id", "method", "path", name="uq_mock_project_route"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    organization_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    project_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=True, index=True,
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    method: Mapped[str] = mapped_column(String(10), nullable=False, default="GET")
    path: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # ── 回應 ─────────────────────────────────────────────────
    status_code: Mapped[int] = mapped_column(Integer, nullable=False, default=200)
    delay_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    response_headers_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    response_body_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # ── 試打用範本(送出時帶到 mock server)───────────────────
    request_headers_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    request_body_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )
