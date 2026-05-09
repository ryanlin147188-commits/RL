"""Hermes messaging gateway 憑證(per-user / per-platform Bot token)。

取代「叫使用者去 sidecar 容器內手動編輯 gateway.json」的麻煩 — 讓使用者
在 UI 上輸入 Telegram bot token / Discord bot token 等,backend Fernet 加密
存 DB,推給 sidecar 寫入 `<HERMES_HOME>/gateway.json`。

設計取捨:
- Per (owner, platform) 一筆。同一個使用者一個平台只能一個 token(禁多 token)
- platform 用自由字串(對齊 Hermes Platform enum 但沒綁死),從 'telegram'
  開始,後續加 'discord' / 'slack' 等都是新增 row,schema 不變
- bot_token 用 Fernet 加密(EncryptedString descriptor,跟 ai_token_configs 同
  pattern)— 解密只在 backend 容器內,plaintext 只在推給 sidecar 時瞬時存在
- enabled 旗標分開存:可以保留 token 但暫時關掉(使用者偶爾想停 daemon)
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.auth.crypto import EncryptedString

from .base import Base


class HermesGatewayCredential(Base):
    __tablename__ = "hermes_gateway_credentials"
    __table_args__ = (
        UniqueConstraint("owner", "platform", name="uq_hermes_gateway_owner_platform"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4()),
    )
    organization_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    # username,沿用其他 hermes_session_refs 等表的慣例
    owner: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    # 'telegram' / 'discord' / 'slack' / ...(對齊 Hermes Platform enum value)
    platform: Mapped[str] = mapped_column(String(40), nullable=False)
    # Bot token / API key — Fernet 加密存
    bot_token: Mapped[Optional[str]] = mapped_column(EncryptedString(800), nullable=True)
    # 平台特定設定(allowed_users / allow_all / chat_ids 等;先留 JSON 不做 schema)
    extra_config: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    # 旗標:是否目前啟用(false = 保留 token 但不跑 daemon)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow,
    )
