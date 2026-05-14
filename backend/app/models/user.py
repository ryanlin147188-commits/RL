"""User 後端使用者 ORM Model。

從原本 client-side localStorage 改為後端 DB-backed users 表，搭配
JWT 認證。沒有 user_id；username 為主鍵 (天然 unique)。

password_hash 使用 bcrypt（passlib），不存明文密碼。
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class User(Base):
    __tablename__ = "users"

    # v1.1.7 Phase 7: id (UUID) 升格成 PK;username 保留為 NOT NULL UNIQUE,
    # 既有 6 個 ForeignKey("users.username") 繼續有效,不必動 30+ application
    # files 跟 SPA 100+ URL pattern。
    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        server_default=text("gen_random_uuid()::text"),
    )
    username: Mapped[str] = mapped_column(
        String(80), nullable=False, unique=True, index=True,
    )
    display_name: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    # SeaweedFS URL(/pics/avatars/<uuid>.jpg);空 → 用 username 首字當文字頭像
    avatar_url: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("roles.id", ondelete="SET NULL"), nullable=True
    )
    organization_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True, index=True,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_superuser: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # 首次登入(或被管理員 reset 後)強制改密碼前不能呼叫其他 API。
    # bootstrap admin (admin/admin123) 由 lifespan 種出來時 default True。
    must_change_password: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false",
    )
    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    # Agent runtime 偏好:'hermes' / 'openclaw' / NULL=auto
    # 能力 gating 在應用層;DB 不約束(使用者刪光 token 後仍要讀得到舊偏好)。
    preferred_agent: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    # v1.1.5 OIDC 整合:第一次走 SSO 後紀錄 IdP 名稱 + stable subject。
    # NULL = 純本地密碼帳號;``(provider, subject)`` 一組 partial unique
    # index(migration 0024 建立)防止同一個 IdP 重複綁不同 row。
    oidc_provider: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    oidc_subject: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    # 強制讓在飛的舊 token 作廢 — middleware 比對 JWT payload 的 ``gen`` <
    # user.token_generation 直接 401。預設 0 不檢查。
    token_generation: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )
