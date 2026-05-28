"""三家供應商的 list-models 實作 — 走官方 GET endpoint。

不放在各 provider 主檔的理由:list-models 是「設定 UI 用的查詢」, 跟 chat
本身語意不同;集中在這檔便於統一處理 + capabilities 套用。

回傳格式統一:
    [
      {
        "id": "claude-opus-4-7",
        "label": "Claude Opus 4.7",
        "supports_thinking": True,
        "thinking_levels": [{"value": "off", "label": "關閉…"}, ...],
      },
      ...
    ]
"""
from __future__ import annotations

import httpx

from app.llm.errors import (
    LLMAuthError,
    LLMBadRequestError,
    LLMServerError,
    LLMTimeoutError,
)
from app.llm.model_catalog import supports_thinking, thinking_levels_for

# 過濾掉非 chat-capable 的 model(embeddings / TTS / image / moderation 等)
_OPENAI_CHAT_PREFIXES = ("gpt-", "o1", "o3", "o4", "chatgpt-")
_OPENAI_EXCLUDE_KEYWORDS = (
    "embedding",
    "tts",
    "whisper",
    "dall-e",
    "moderation",
    "audio",
    "image",
    "transcribe",
    "realtime",
    "search",
)


def _to_unified(provider: str, model_id: str, label: str) -> dict:
    return {
        "id": model_id,
        "label": label or model_id,
        "supports_thinking": supports_thinking(provider, model_id),
        "thinking_levels": thinking_levels_for(provider, model_id),
    }


async def _get_json(
    url: str,
    *,
    headers: dict[str, str],
    timeout: float,
    provider: str,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict:
    """GET JSON + 共用 error → LLMError 對映(對齊 _http.py 的 POST 版本)。"""
    try:
        async with httpx.AsyncClient(timeout=timeout, transport=transport) as client:
            resp = await client.get(url, headers=headers)
    except httpx.TimeoutException as e:
        raise LLMTimeoutError(str(e), provider=provider) from e
    except httpx.HTTPError as e:
        raise LLMServerError(f"HTTP error: {e}", provider=provider) from e

    if resp.status_code in (401, 403):
        raise LLMAuthError(resp.text[:500], provider=provider)
    if 500 <= resp.status_code < 600:
        raise LLMServerError(
            resp.text[:500], provider=provider, status_code=resp.status_code
        )
    if 400 <= resp.status_code < 500:
        raise LLMBadRequestError(
            resp.text[:500], provider=provider, status_code=resp.status_code
        )
    return resp.json()


async def list_anthropic_models(
    api_key: str,
    *,
    timeout: float = 15.0,
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[dict]:
    """Anthropic Models endpoint:GET /v1/models。"""
    data = await _get_json(
        "https://api.anthropic.com/v1/models?limit=100",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
        timeout=timeout,
        provider="anthropic",
        transport=transport,
    )
    out = []
    for m in data.get("data", []):
        mid = m.get("id")
        if not mid:
            continue
        # 跳過 deprecated / preview internal models(用 type 過濾就好)
        if m.get("type") and m.get("type") != "model":
            continue
        display = m.get("display_name") or mid
        out.append(_to_unified("anthropic", mid, display))
    # 排序:最新 / opus 排前
    out.sort(key=lambda x: (
        0 if "opus" in x["id"] else 1 if "sonnet" in x["id"] else 2,
        x["id"],
    ))
    return out


async def list_openai_models(
    api_key: str,
    *,
    base_url: str | None = None,
    timeout: float = 15.0,
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[dict]:
    """OpenAI / OpenAI-compatible:GET /v1/models。若 base_url 指向本地推論
    (vLLM / Ollama),會列本地可用 model 而非官方。"""
    if base_url:
        # base_url 可能是 chat completion endpoint,把尾巴 /chat/completions 去掉
        url_root = base_url.rstrip("/").replace("/chat/completions", "")
        url = url_root + "/models"
    else:
        url = "https://api.openai.com/v1/models"
    data = await _get_json(
        url,
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=timeout,
        provider="openai",
        transport=transport,
    )
    out = []
    for m in data.get("data", []):
        mid = m.get("id", "")
        low = mid.lower()
        if not mid:
            continue
        # 只保留 chat-capable 模型(過濾 embedding / TTS / image / moderation)
        if not any(low.startswith(p) for p in _OPENAI_CHAT_PREFIXES):
            continue
        if any(k in low for k in _OPENAI_EXCLUDE_KEYWORDS):
            continue
        out.append(_to_unified("openai", mid, mid))
    out.sort(key=lambda x: x["id"])
    return out


async def list_google_models(
    api_key: str,
    *,
    timeout: float = 15.0,
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[dict]:
    """Google AI Studio:GET /v1beta/models?key=…(filter generateContent)。"""
    url = f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}"
    data = await _get_json(
        url, headers={}, timeout=timeout, provider="google", transport=transport
    )
    out = []
    for m in data.get("models", []):
        # 過濾出支援 generateContent(即 chat)的模型
        if "generateContent" not in (m.get("supportedGenerationMethods") or []):
            continue
        full = m.get("name", "")  # "models/gemini-2.5-pro"
        mid = full.split("/", 1)[1] if "/" in full else full
        display = m.get("displayName") or mid
        if not mid:
            continue
        # 跳掉非 gemini 系列(palm / chat-bison 等舊式)
        if not mid.startswith("gemini"):
            continue
        out.append(_to_unified("google", mid, display))
    out.sort(key=lambda x: x["id"])
    return out


PROVIDER_LISTERS = {
    "anthropic": list_anthropic_models,
    "openai": list_openai_models,
    "google": list_google_models,
}
