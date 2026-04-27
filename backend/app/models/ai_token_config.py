"""AiTokenConfig AI Token 設定 ORM Model — 多個 provider 並存。

provider 為自由文字(常見:OpenAI / Anthropic / Google / DeepSeek / Groq /
OpenRouter / Together / Mistral / xAI / Cohere / 任何 OpenAI-compatible 本地)。
base_url 不再由使用者填,改由後端依 provider 名稱對應(見 ai_provider_map.py)。
"""
from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.auth.crypto import EncryptedString

from .base import Base


# 保留 enum class 給舊 import 不出錯;新欄位用自由 String
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
    # 自由文字(不再是 enum):OpenAI / Anthropic / DeepSeek / Groq / OpenRouter / ...
    # base_url 由後端根據此名字推算(見 services/ai_provider_map.py)
    provider: Mapped[str] = mapped_column(String(40), nullable=False, default="OpenAI")
    # api_key 可空(Local provider 通常不需要);Fernet 加密儲存
    api_key: Mapped[Optional[str]] = mapped_column(EncryptedString(800), nullable=True)
    # base_url:保留欄位給「自架 / OpenAI-compatible 自訂端點」使用;前端不顯示,
    # 預設依 provider name 推算。若使用者進階設定填了會覆蓋。
    base_url: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    model: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    # 思考程度(僅 OpenAI o1/o3 等推理模型支援):low / medium / high
    reasoning_effort: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    description: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )
