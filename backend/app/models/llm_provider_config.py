"""LlmProviderConfig ORM Model — 每個 organization 對每家 LLM 供應商各
存一筆設定(api_key Fernet 加密)。

設計沿用 EmailConfig 模式:
* per-organization,UNIQUE(organization_id, provider)
* api_key 用 ``EncryptedString`` TypeDecorator:寫入自動加密、讀取自動解密
* ``provider`` 用字串而非 Enum,避免新增供應商時要做 schema migration
* 留 ``base_url`` 給 OpenAI-compatible 本地推論伺服器
* ``default_model`` 給該 provider 的預設模型 hint;呼叫端仍可 override

Why per-organization 而非 per-user:
* 三家 × 上百 user 太難管,計費歸屬也難算
* 集中在 org 比較容易做計費紅線 / 月度 budget cap
* 個人 override 是 Phase 1+ 的事(可能加在 user_settings.preferred_model)
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.auth.crypto import EncryptedString

from .base import Base


class LlmProviderConfig(Base):
    __tablename__ = "llm_provider_configs"
    __table_args__ = (
        UniqueConstraint("organization_id", "provider", name="uq_llm_provider_org_provider"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    # nullable=True 留給 "global default"(superuser 設一份給沒設過的 org 用)
    organization_id: Mapped[Optional[str]] = mapped_column(
        String(36),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    # "anthropic" | "openai" | "google"(字串而非 Enum,以利擴充)
    provider: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    # Fernet 加密;ORM 看到的是明文,DB 內是 "fernet:gAAAA..." 格式
    api_key: Mapped[Optional[str]] = mapped_column(EncryptedString(500), nullable=True)
    # 給 UI 顯示用的「最後可看一眼」遮罩(類似 ApiKey 模式)。
    # 格式:raw key 前 7~8 個非 hash 字元 + "***" 結尾數字。永遠不存完整 key。
    # 例:Anthropic "sk-ant-***bef9" / OpenAI "sk-proj-***a7c" / Google "AIza***Xyz"
    key_prefix: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    # OpenAI-compatible 本地推論伺服器才用得到;其他家留空
    base_url: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    # 該 provider 的預設模型(例如 OpenAI 設成 gpt-4o-mini,Anthropic 設成 claude-opus-4-7)
    default_model: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    # v1.2.x:統一思考度設定。格式 {"level": "off"|"low"|"medium"|"high"}。
    # off 或 None = chat 時不傳 thinking field;low/medium/high → provider adapter
    # 轉成各家對應參數(Anthropic budget_tokens / OpenAI reasoning_effort /
    # Google thinkingBudget)。新欄位 nullable,舊 row 不影響。
    thinking_config: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )
