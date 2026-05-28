"""LLM provider config REST endpoints — Phase 0 後半段。

每 organization × provider 一筆。所有 endpoint 走 Casbin SETTINGS_READ / WRITE
權限(不另外加 permission code,沿用 EmailConfig 慣例)。

紅線:
* api_key 寫入後永遠不從 Response 回傳,UI 只能看到 has_api_key bool
* update 時空字串視為「不動」,要清除走 DELETE
* test endpoint 走真的 chat 一次(包 timeout),驗 key 確實有效
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Path
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.auth.permissions import require_casbin
from app.auth.permissions_catalog import P
from app.database import get_db
from app.llm.base import Message, Role
from app.llm.errors import LLMError
from app.models.llm_provider_config import LlmProviderConfig
from app.models.user import User
from app.schemas.llm_provider import (
    ALLOWED_PROVIDERS,
    LlmProviderConfigResponse,
    LlmProviderConfigUpdate,
    LlmProviderTestRequest,
    LlmProviderTestResponse,
    ProviderName,
)
from app.services import llm_config_service

router = APIRouter()


def _to_response(row: LlmProviderConfig) -> dict:
    """永不回 api_key 明碼;以 has_api_key bool + key_prefix 遮罩取代。"""
    return {
        "id": row.id,
        "organization_id": row.organization_id,
        "provider": row.provider,
        "base_url": row.base_url,
        "default_model": row.default_model,
        "enabled": row.enabled,
        "has_api_key": bool(row.api_key),
        "key_prefix": row.key_prefix,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _validate_provider(provider: str) -> None:
    if provider not in ALLOWED_PROVIDERS:
        raise HTTPException(
            422, f"provider 必須是 {ALLOWED_PROVIDERS} 其中之一,收到:{provider!r}"
        )


def _require_superuser(user: User) -> None:
    """global default(organization_id IS NULL)是給「沒設過 key 的 org」用的
    fallback,任何 org 都會 read。只有 superuser 可以動,否則一個 org admin
    可以「順便」幫所有 org 設預設 key,違反多租戶界線。"""
    if not user.is_superuser:
        raise HTTPException(403, "需要 superuser 權限才能設定 global default")


@router.get(
    "/settings/llm-providers",
    response_model=list[LlmProviderConfigResponse],
    tags=["S · 設定"],
    dependencies=[Depends(require_casbin(P.SETTINGS_READ))],
)
async def list_llm_providers(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    rows = await llm_config_service.list_for_org(db, user.organization_id)
    return [_to_response(r) for r in rows]


@router.get(
    "/settings/llm-providers/{provider}",
    response_model=LlmProviderConfigResponse,
    tags=["S · 設定"],
    dependencies=[Depends(require_casbin(P.SETTINGS_READ))],
)
async def get_llm_provider(
    provider: ProviderName = Path(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _validate_provider(provider)
    row = await llm_config_service.get_for_org(db, user.organization_id, provider)
    if row is None:
        raise HTTPException(404, f"尚未設定 {provider} 的 API key")
    return _to_response(row)


@router.put(
    "/settings/llm-providers/{provider}",
    response_model=LlmProviderConfigResponse,
    tags=["S · 設定"],
    dependencies=[Depends(require_casbin(P.SETTINGS_WRITE))],
)
async def upsert_llm_provider(
    payload: LlmProviderConfigUpdate,
    provider: ProviderName = Path(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _validate_provider(provider)
    try:
        row = await llm_config_service.upsert_for_org(
            db, user.organization_id, provider, payload
        )
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    return _to_response(row)


@router.delete(
    "/settings/llm-providers/{provider}",
    status_code=204,
    tags=["S · 設定"],
    dependencies=[Depends(require_casbin(P.SETTINGS_WRITE))],
)
async def delete_llm_provider(
    provider: ProviderName = Path(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _validate_provider(provider)
    deleted = await llm_config_service.delete_for_org(
        db, user.organization_id, provider
    )
    if not deleted:
        raise HTTPException(404, f"尚未設定 {provider} 的 API key")
    return None


@router.post(
    "/settings/llm-providers/{provider}/test",
    response_model=LlmProviderTestResponse,
    tags=["S · 設定"],
    dependencies=[Depends(require_casbin(P.SETTINGS_WRITE))],
)
async def test_llm_provider(
    payload: LlmProviderTestRequest,
    provider: ProviderName = Path(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """用一個小 prompt 驗剛存的 key 有效。

    走真的 chat() 一次;LLMError 統一轉成 200 OK + ok=False + error 訊息
    (而非 5xx),讓前端能正常顯示「key 無效」這類使用者可修錯誤,而不是
    噴 500 stacktrace。
    """
    _validate_provider(provider)

    # 決定要打哪個 model:caller > DB.default_model > provider hardcode default
    default_per_provider = {
        "anthropic": "claude-haiku-4-5-20251001",
        "openai": "gpt-4o-mini",
        "google": "gemini-2.5-flash",
    }
    row = await llm_config_service.get_for_org(db, user.organization_id, provider)
    model = (
        payload.model
        or (row.default_model if row else None)
        or default_per_provider[provider]
    )

    try:
        llm = await llm_config_service.resolve_provider(
            db, provider_name=provider, organization_id=user.organization_id
        )
    except ValueError as e:
        return LlmProviderTestResponse(
            ok=False, model=model, provider=provider,
            sample_text="", input_tokens=0, output_tokens=0, cost_usd=0.0,
            error=str(e),
        )

    prompt_text = (payload.prompt or "ping").strip() or "ping"
    try:
        result = await llm.chat(
            [Message(Role.USER, prompt_text)],
            model=model,
            max_tokens=64,
            temperature=0.0,
            timeout=15.0,
        )
    except LLMError as e:
        return LlmProviderTestResponse(
            ok=False, model=model, provider=provider,
            sample_text="", input_tokens=0, output_tokens=0, cost_usd=0.0,
            error=f"[{type(e).__name__}] {e}",
        )

    return LlmProviderTestResponse(
        ok=True,
        model=result.model,
        provider=provider,
        sample_text=result.content_text[:200],
        input_tokens=result.usage.input_tokens,
        output_tokens=result.usage.output_tokens,
        cost_usd=result.usage.cost_usd,
    )


# ─── Superuser-only: global default(organization_id IS NULL) ─────────
# 這份 row 是「沒設過 key 的 org 用的 fallback」,任何 org 都會 read 到,
# 因此只給 superuser 動。資料路徑跟 org-level 完全平行,只是 organization_id=None。


@router.get(
    "/admin/llm-providers/global",
    response_model=list[LlmProviderConfigResponse],
    tags=["X · 組織"],
)
async def list_global_llm_providers(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _require_superuser(user)
    rows = await llm_config_service.list_for_org(db, None)
    return [_to_response(r) for r in rows]


@router.get(
    "/admin/llm-providers/global/{provider}",
    response_model=LlmProviderConfigResponse,
    tags=["X · 組織"],
)
async def get_global_llm_provider(
    provider: ProviderName = Path(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _require_superuser(user)
    _validate_provider(provider)
    row = await llm_config_service.get_for_org(db, None, provider)
    if row is None:
        raise HTTPException(404, f"尚未設定 global default {provider}")
    return _to_response(row)


@router.put(
    "/admin/llm-providers/global/{provider}",
    response_model=LlmProviderConfigResponse,
    tags=["X · 組織"],
)
async def upsert_global_llm_provider(
    payload: LlmProviderConfigUpdate,
    provider: ProviderName = Path(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _require_superuser(user)
    _validate_provider(provider)
    try:
        row = await llm_config_service.upsert_for_org(db, None, provider, payload)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    return _to_response(row)


@router.delete(
    "/admin/llm-providers/global/{provider}",
    status_code=204,
    tags=["X · 組織"],
)
async def delete_global_llm_provider(
    provider: ProviderName = Path(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _require_superuser(user)
    _validate_provider(provider)
    deleted = await llm_config_service.delete_for_org(db, None, provider)
    if not deleted:
        raise HTTPException(404, f"尚未設定 global default {provider}")
    return None
