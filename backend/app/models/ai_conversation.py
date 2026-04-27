"""AI 對話 ORM model — 兩張表:`ai_conversations` 跟 `ai_messages`。

- AiConversation:一個 chat session 的 metadata,屬於某 user
- AiMessage:每一輪 user / assistant message,以 conversation_id 串起來

可重用既有的 `ai_token_configs` 取得 LLM provider 設定;這邊只管對話歷史。
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class AiConversation(Base):
    __tablename__ = "ai_conversations"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    organization_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    # 開啟對話的使用者(透過 username,跟 notification 同 pattern)
    owner: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    # 顯示用標題,前端可改;空 → 由前端根據首則 user message 自動命名
    title: Mapped[str] = mapped_column(String(200), nullable=False, default="新對話")
    # 對話採用的 AI provider 設定 id(對應 ai_token_configs.id);空 = 系統預設
    provider_config_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class AiMessage(Base):
    __tablename__ = "ai_messages"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    conversation_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("ai_conversations.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    # 'user' / 'assistant' / 'system'(目前不存 system,留位給未來可調整 prompt)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # 可選:LLM 回傳的 token 使用量 + 哪個 provider/model 處理
    tokens_used: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    provider: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    model: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
