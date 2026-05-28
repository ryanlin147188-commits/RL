"""依 model_id 路由到對應 provider。

Phase 0 後半段:provider 實例的來源從 env-only 升級成
``llm_config_service.resolve_provider()`` — DB 優先、env fallback。

兩種 caller 入口:
* ``infer_provider(model)``:純字串判斷,給呼叫端決定要走哪家(同步、無 IO)
* ``get_provider_for_chat(db, model, org_id)``:取得真的能用的 provider 實例
  (有 DB session 才能用;在 routers / services 用)

舊版 ``get_provider(model)`` 改成只支援 env-only 模式,給 Celery worker
等沒有 db session 的場景 fallback 用。
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from app.llm.base import LLMProvider

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


def infer_provider(model: str) -> str:
    """依 model id 前綴判斷供應商。命中不到 raise ValueError。

    保留 ``gpt`` / ``o1`` / ``o3`` / ``o4`` 前綴給 OpenAI;``claude`` 給
    Anthropic;``gemini`` 給 Google。本地 OpenAI-compatible 推論可在 caller
    端 hardcode ``"openai"`` 並指定 base_url 覆寫。
    """
    m = model.lower()
    if m.startswith("claude"):
        return "anthropic"
    if m.startswith(("gpt", "o1", "o3", "o4", "chatgpt")):
        return "openai"
    if m.startswith("gemini"):
        return "google"
    raise ValueError(
        f"無法從 model id '{model}' 推斷 provider。"
        " 支援前綴:claude- / gpt- / o1/o3/o4- / gemini-"
    )


async def get_provider_for_chat(
    db: "AsyncSession",
    model: str,
    *,
    organization_id: Optional[str],
) -> LLMProvider:
    """根據 model + org 取得目前可用的 provider 實例。

    DB 優先:該 org 有設 → 用該 org 的;否則 fallback 到 global default;
    最後 fallback 到 env 變數。任一層都找不到時 raise ValueError。

    呼叫端負責處理 ValueError(通常轉 400)。Auth 紅線:此函式假設 caller
    已經做完 Casbin 權限檢查,本身不再驗權。
    """
    # 延遲 import 避免 circular(service → router → service ...)
    from app.services.llm_config_service import resolve_provider

    provider_name = infer_provider(model)
    return await resolve_provider(
        db, provider_name=provider_name, organization_id=organization_id
    )


def get_provider(model: str) -> LLMProvider:
    """Env-only fallback,給沒有 DB session 的場景(例如 Celery worker 內
    的 retry / fallback path)。優先用 ``get_provider_for_chat()``。
    """
    provider_name = infer_provider(model)
    # 直接走 service 的 env builder;不開 DB session
    from app.services.llm_config_service import _build_from_env

    return _build_from_env(provider_name)
