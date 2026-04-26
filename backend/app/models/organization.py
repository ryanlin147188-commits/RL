"""Organization 組織（租戶）ORM Model。

shared-DB + tenant_id 模式：
- 每個資源（Project / Role / EmailConfig / AiTokenConfig / TodoItem / AuditLog）
  都帶 organization_id 作為硬隔離鍵
- User 也屬於某 org；登入後 JWT 內會有 org_id；所有查詢以該 org_id 過濾

預設行為：
- 啟動時若沒有任何 organization，自動建立 ``Default Organization`` (slug=default)
- 既有資料的 organization_id 為 NULL → middleware 端 fallback 到 default org
- Superuser 可建立 / 切換多個 organization；普通使用者一律綁定到自己的 org
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    slug: Mapped[str] = mapped_column(String(60), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    plan: Mapped[Optional[str]] = mapped_column(String(40), nullable=True, default="free")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )
