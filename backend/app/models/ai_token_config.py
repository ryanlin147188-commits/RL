"""AiTokenConfig AI Token 設定 ORM Model — 多個 provider 並存。

支援：
- OpenAI (GPT-3.5/4/4o)
- Anthropic (Claude)
- Google (Gemini)
- Local (Ollama / vLLM / LMStudio 等本地推論伺服器，透過 OpenAI-compatible API)
"""
from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.auth.crypto import EncryptedString

from .base import Base


class AiProvider(str, enum.Enum):
    OPENAI = "OpenAI"
    ANTHROPIC = "Anthropic"
    GOOGLE = "Google"
    LOCAL = "Local"


class AiTokenConfig(Base):
    __tablename__ = "ai_token_configs"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    organization_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True, index=True,
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    provider: Mapped[AiProvider] = mapped_column(
        Enum(AiProvider), nullable=False, default=AiProvider.OPENAI
    )
    # api_key 可空（Local provider 通常不需要）；用 Fernet 加密儲存
    api_key: Mapped[Optional[str]] = mapped_column(EncryptedString(800), nullable=True)
    base_url: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    model: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    description: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )
