"""把 ai_token_configs 的 token 攤平成 mem0 sidecar 需要的 llm_config / embedder_config。

mem0 lib 的 Memory.from_config 接受 schema:
    {"provider": "openai|anthropic|gemini|deepseek|...", "config": {api_key, model, ...}}

這個檔案做兩件事:
1. 從 AiTokenConfig(自由字串 provider + Fernet 解後 plaintext key)解出 mem0 expected dict
2. 對 Anthropic-only user(沒 OpenAI-compatible embedder)回 None,讓上游降級

對齊 ai_provider_map.py 既有的 provider→base_url 對應表 — 不重發明輪子。
"""
from __future__ import annotations

from typing import Optional

from app.models.ai_token_config import AiTokenConfig
from app.services.ai_provider_map import resolve

# mem0 lib v0.1.x 認得的 LLM provider(與 ai_token_configs.provider 自由字串對映)
# 來源:mem0/utils/factory.py:LlmFactory.provider_to_class.keys()
_MEM0_LLM_PROVIDERS = {
    "openai", "anthropic", "gemini", "deepseek", "groq", "together",
    "ollama", "litellm", "azure_openai", "aws_bedrock", "xai", "lmstudio",
}

# mem0 lib 認得的 embedder provider(僅 OpenAI-compatible 與本地)
_MEM0_EMBEDDER_PROVIDERS = {
    "openai", "azure_openai", "ollama", "huggingface", "gemini",
    "vertexai", "together", "lmstudio",
}

# OpenAI 官方 endpoint;OpenAI-compatible 第三方(deepseek/groq/openrouter)
# 不一定有 embedding endpoint(尤其 anthropic/groq),要 caller 自己處理。
_DEFAULT_OPENAI_EMBED_MODEL = "text-embedding-3-small"
# 對齊 mem0 lib 預設(1536 維;mem0_proxy.py 的 EMBEDDING_DIMS 也是 1536)
# 切 3-large(3072)會不相容,要重起 sidecar + 改 collection。


def _normalize_provider(raw: str) -> str:
    """ai_token_configs.provider 是自由字串(常 'OpenAI' / 'Anthropic' / 'OpenRouter'),
    對應到 mem0 / ai_provider_map 的小寫 key。
    """
    return (raw or "").strip().lower()


def build_llm_config(token: AiTokenConfig) -> dict:
    """從 AiTokenConfig 攤平成 mem0 LLM config。

    對 mem0 認得的 provider 直接用;OpenAI-compatible 第三方(openrouter/together
    等)落到 'openai' 並透過 base_url override 走他們的 endpoint。
    """
    raw_provider = _normalize_provider(token.provider)
    spec = resolve(token.provider, base_url_override=token.base_url)

    # 對 mem0 直接認得的 provider 用 native config
    if raw_provider == "anthropic":
        return {
            "provider": "anthropic",
            "config": {
                "api_key": token.api_key,
                "model": token.model or "claude-3-5-haiku-latest",
            },
        }
    if raw_provider in ("gemini", "google"):
        return {
            "provider": "gemini",
            "config": {
                "api_key": token.api_key,
                "model": token.model or "gemini-2.0-flash",
            },
        }
    if raw_provider in _MEM0_LLM_PROVIDERS and raw_provider != "openai":
        # 直接認得的(deepseek/groq/together/...)用其 native key 名,讓 mem0
        # 走它的官方 SDK
        return {
            "provider": raw_provider,
            "config": {
                "api_key": token.api_key,
                "model": token.model or "",
            },
        }

    # OpenAI 與其他 OpenAI-compatible 第三方(openrouter/xai/groq via base_url)
    # 走 'openai' provider + openai_base_url override(mem0 lib 支援)
    cfg: dict = {
        "api_key": token.api_key,
        "model": token.model or "gpt-4o-mini",
    }
    # spec.style == 'openai-compat' → spec.base_url 是該 provider 的 endpoint
    if spec.style == "openai-compat":
        cfg["openai_base_url"] = spec.base_url
    return {"provider": "openai", "config": cfg}


def build_embedder_config(token: AiTokenConfig) -> Optional[dict]:
    """從 AiTokenConfig 攤平成 mem0 embedder config。

    mem0 embedder 限定:OpenAI / Azure OpenAI / Ollama / HuggingFace / Gemini /
    VertexAI / Together / LMStudio。Anthropic 沒 embedder,這時回 None;
    上游 send_message 看到 None 會降級成「不做 mem0 hook」或「只 add(LLM extraction)
    不 search」。
    """
    raw_provider = _normalize_provider(token.provider)
    spec = resolve(token.provider, base_url_override=token.base_url)

    # 純 Anthropic / 純 Cohere 等沒 embedder API 的 provider — 直接降級
    if raw_provider in ("anthropic",):
        return None

    # Gemini 有 embedder(但跟 LLM 同 key)
    if raw_provider in ("gemini", "google"):
        return {
            "provider": "gemini",
            "config": {
                "api_key": token.api_key,
                "model": "models/text-embedding-004",
                "embedding_dims": 1536,  # 對齊 sidecar 設定
            },
        }

    # Ollama / LMStudio 走 native(本地嵌入)
    if raw_provider in ("ollama", "lmstudio") and raw_provider in _MEM0_EMBEDDER_PROVIDERS:
        cfg: dict = {
            "model": token.model or "nomic-embed-text",
        }
        if spec.style == "openai-compat":
            # ollama 走 OpenAI-compat 用 openai-style embed call
            cfg["ollama_base_url"] = spec.base_url
        return {"provider": raw_provider, "config": cfg}

    # 其他 OpenAI-compatible(含 OpenAI 官方、DeepSeek、Groq、OpenRouter、xAI 等)
    # 走 'openai' provider — 但**注意**:DeepSeek / Groq / xAI 沒 embedder 端點,
    # 會在實際 mem0 search 時失敗。這層只保證「config dict 合法」,真實可用性
    # 由上游 graceful degradation 接(plan §6)。
    if spec.style == "openai-compat":
        cfg = {
            "api_key": token.api_key,
            "model": _DEFAULT_OPENAI_EMBED_MODEL,
            "embedding_dims": 1536,
        }
        if raw_provider != "openai":
            cfg["openai_base_url"] = spec.base_url
        return {"provider": "openai", "config": cfg}

    # 完全不認識的 provider — 安全起見回 None,讓上游知道沒法做 mem0
    return None
