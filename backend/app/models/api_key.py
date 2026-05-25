"""ApiKey ORM Model — v1.1.10 加。

長壽命的 API token,讓 CI/CD pipeline / 外部整合不必拿短命 JWT(15min)反覆
refresh。Gateway 端看到 ``X-API-Key: ak_xxxxxxxxxxxxxxx`` 就 SHA256 hash
查這個表;比對 key_hash 通過後 mint 一個 5 分鐘 JWT 給 backend 用。

Security:
* 只存 SHA256(key);明碼只在 POST /api/auth/api-keys 回應時回一次,user 必
  須記下來,之後再也拿不到(類似 GitHub PAT)
* ``key_prefix``(前 8 字)給 UI 顯示「最後可看一眼」
* ``scopes`` 可選 limit 該 key 能做的事(目前 backend 沒接 scope enforcement;
  Commit 4+ iteration 才整)
* ``revoked`` flag + ``expires_at`` 都可立刻擋
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from app.auth.tenant import TenantScoped
from .base import Base


class ApiKey(TenantScoped, Base):
    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    # SHA256 of plain key — 永遠不存明碼
    key_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True,
    )
    # 前 8 字(含 ak_ prefix),只是給 UI 顯示用
    key_prefix: Mapped[str] = mapped_column(String(16), nullable=False)
    # 人讀名稱(像 GitHub PAT 的 "deploy bot")
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    # 誰 own 這把 key — FK 到 users.id(UUID)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    # 可選 scope 列表(JSON array of strings)— 目前 backend 沒接,未來可選
    scopes: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    revoked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow,
    )
