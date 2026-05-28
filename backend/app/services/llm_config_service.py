"""LlmProviderConfig service — CRUD 與「給定 provider 取得可用 LLMProvider 實例」。

兩種 caller:
1. **routers/llm_providers.py**:CRUD endpoints,只動 DB row
2. **llm/router.py**:在 chat 前根據 model_id 取 provider 實例;優先讀 DB,
   找不到再 fallback 到 env(app.config.settings)

優先順序設計:
    DB row(org-scoped, enabled=True) > DB row(global default, enabled=True) > env

「global default」= organization_id IS NULL 的那筆;superuser 設一份給沒設過
的 org 用。Phase 0 後半段只實作前兩層;global default 留結構,等 admin UI 出來。
"""
from __future__ import annotations

import uuid
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.llm.base import LLMProvider
from app.llm.providers import AnthropicProvider, GoogleProvider, OpenAIProvider
from app.models.llm_provider_config import LlmProviderConfig
from app.schemas.llm_provider import ALLOWED_PROVIDERS, LlmProviderConfigUpdate


_PROVIDER_CLASSES: dict[str, type[LLMProvider]] = {
    "anthropic": AnthropicProvider,
    "openai": OpenAIProvider,
    "google": GoogleProvider,
}


def _compute_key_prefix(raw_key: str) -> str:
    """從原始 key 算出 UI 顯示用的遮罩字串。

    規則:取前 8 字 + "***" + 末 4 字(若 key 夠長);太短就只回前綴 + ***。
    例:
    * "sk-ant-api03-aBcDe...XyZ" → "sk-ant-a***XyZw"
    * "AIzaSyBxxxxxxxxxxxxxxxx" → "AIzaSyBx***xxxx"
    * "短"(< 12) → "短***"

    這是純展示字串,即使外洩也不該影響資安(只有前後共 ~12 字)。
    """
    raw_key = raw_key.strip()
    if not raw_key:
        return ""
    if len(raw_key) < 12:
        return raw_key[:4] + "***"
    return raw_key[:8] + "***" + raw_key[-4:]


async def list_for_org(
    db: AsyncSession, organization_id: Optional[str]
) -> list[LlmProviderConfig]:
    """列出該 org 的所有 provider 設定(含未啟用),不含 global default。"""
    stmt = select(LlmProviderConfig).where(
        LlmProviderConfig.organization_id == organization_id
    )
    rows = (await db.execute(stmt)).scalars().all()
    return list(rows)


async def get_for_org(
    db: AsyncSession,
    organization_id: Optional[str],
    provider: str,
) -> Optional[LlmProviderConfig]:
    """取單一 (org, provider) 對應的設定;None 表沒設過。"""
    stmt = select(LlmProviderConfig).where(
        LlmProviderConfig.organization_id == organization_id,
        LlmProviderConfig.provider == provider,
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def upsert_for_org(
    db: AsyncSession,
    organization_id: Optional[str],
    provider: str,
    payload: LlmProviderConfigUpdate,
) -> LlmProviderConfig:
    """Upsert 一筆 provider config。

    api_key 為 None / 空字串 → 不動既有值(沿用 EmailConfig 慣例)。
    要清除 api_key 請走 ``delete_for_org``。
    """
    if provider not in ALLOWED_PROVIDERS:
        raise ValueError(f"未知 provider: {provider}")
    if provider != payload.provider:
        raise ValueError("URL provider 與 payload.provider 不一致")

    row = await get_for_org(db, organization_id, provider)
    if row is None:
        row = LlmProviderConfig(
            id=str(uuid.uuid4()),
            organization_id=organization_id,
            provider=provider,
        )
        db.add(row)

    row.base_url = payload.base_url
    row.default_model = payload.default_model
    row.enabled = payload.enabled
    # 空字串 / None = 不動;非空 = 寫入(EncryptedString 會自動 Fernet 加密)
    # 同時更新 key_prefix 給 UI 顯示遮罩(永遠不存完整 key)
    if payload.api_key:
        row.api_key = payload.api_key
        row.key_prefix = _compute_key_prefix(payload.api_key)

    await db.flush()
    await db.refresh(row)
    return row


async def delete_for_org(
    db: AsyncSession,
    organization_id: Optional[str],
    provider: str,
) -> bool:
    """刪除一筆 provider config(含 api_key)。回傳是否實際刪到。"""
    row = await get_for_org(db, organization_id, provider)
    if row is None:
        return False
    await db.delete(row)
    await db.flush()
    return True


async def resolve_provider(
    db: AsyncSession,
    *,
    provider_name: str,
    organization_id: Optional[str],
) -> LLMProvider:
    """根據 provider_name 取得「目前可用」的 LLMProvider 實例。

    優先順序:
        1. DB(org_id=organization_id, enabled=True, 有 api_key)
        2. DB(org_id IS NULL, enabled=True, 有 api_key) — global default
        3. app.config.settings 對應的 env 變數

    找不到任何來源 → raise ValueError(由 router 轉成 400 / 422)。
    每次呼叫都建新 provider 實例(api_key 可能換,不能 lru_cache;httpx
    Client 在 chat() 內 ``async with`` 自己管生命週期)。
    """
    if provider_name not in _PROVIDER_CLASSES:
        raise ValueError(f"未知 provider: {provider_name}")

    # 1) org-scoped row
    row = await get_for_org(db, organization_id, provider_name)
    if row and row.enabled and row.api_key:
        return _build_from_row(row)

    # 2) global default(organization_id IS NULL)
    if organization_id is not None:
        global_row = await get_for_org(db, None, provider_name)
        if global_row and global_row.enabled and global_row.api_key:
            return _build_from_row(global_row)

    # 3) env fallback
    return _build_from_env(provider_name)


def _build_from_row(row: LlmProviderConfig) -> LLMProvider:
    cls = _PROVIDER_CLASSES[row.provider]
    if row.provider == "openai":
        # OpenAI 支援自訂 base_url(本地推論);空字串 / None → 用 SDK default
        base_url = row.base_url or None
        if base_url:
            return cls(api_key=row.api_key, base_url=base_url)  # type: ignore[call-arg]
        return cls(api_key=row.api_key)  # type: ignore[call-arg]
    return cls(api_key=row.api_key)  # type: ignore[call-arg]


def _build_from_env(provider_name: str) -> LLMProvider:
    from app.config import settings

    if provider_name == "anthropic":
        key = settings.ANTHROPIC_API_KEY
        if not key:
            raise ValueError(
                "Anthropic 未設定 API key。請在系統設定填寫,或設環境變數 ANTHROPIC_API_KEY"
            )
        return AnthropicProvider(api_key=key)
    if provider_name == "openai":
        key = settings.OPENAI_API_KEY
        if not key:
            raise ValueError(
                "OpenAI 未設定 API key。請在系統設定填寫,或設環境變數 OPENAI_API_KEY"
            )
        base = settings.OPENAI_BASE_URL or None
        if base:
            return OpenAIProvider(api_key=key, base_url=base)
        return OpenAIProvider(api_key=key)
    if provider_name == "google":
        key = settings.GOOGLE_API_KEY
        if not key:
            raise ValueError(
                "Google 未設定 API key。請在系統設定填寫,或設環境變數 GOOGLE_API_KEY"
            )
        return GoogleProvider(api_key=key)
    raise ValueError(f"未知 provider: {provider_name}")
