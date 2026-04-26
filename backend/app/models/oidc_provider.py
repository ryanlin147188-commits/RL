"""OidcProvider — 每組織可掛多個 OIDC SSO 提供者（Google / Azure AD / Okta / Auth0 等）。

設計：
- 每筆對應一個外部 OIDC IdP；client_id + client_secret 由 IdP 申請
- discovery_url（.well-known/openid-configuration）會自動 fetch authorize / token / jwks 端點
- 也可手動填 authorize_url / token_url / jwks_url（適合非標準 OIDC）
- client_secret 用 Fernet 加密（EncryptedString）
- enabled=False → 登入頁不顯示，避免半完成的設定干擾使用者
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.auth.crypto import EncryptedString

from .base import Base


class OidcProvider(Base):
    __tablename__ = "oidc_providers"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    organization_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=True, index=True,
    )
    # 顯示在登入按鈕上的名稱（例：Google / Azure AD / Company SSO）
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    # 在 URL slug / log 中辨識用（同一 org 內 unique）
    slug: Mapped[str] = mapped_column(String(40), nullable=False)
    # OIDC discovery URL；通常是 https://issuer/.well-known/openid-configuration
    discovery_url: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    # 當 discovery 不可用時手動填
    issuer: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    authorize_url: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    token_url: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    jwks_url: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    # OAuth client 認證
    client_id: Mapped[str] = mapped_column(String(255), nullable=False)
    client_secret: Mapped[Optional[str]] = mapped_column(EncryptedString(800), nullable=True)
    # 預設要的 scope；OIDC 標準是 "openid email profile"
    scopes: Mapped[str] = mapped_column(String(300), nullable=False, default="openid email profile")
    # 登入鈕圖示（FontAwesome class）
    button_icon: Mapped[Optional[str]] = mapped_column(String(80), nullable=True, default="fa-solid fa-key")
    button_label: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )
