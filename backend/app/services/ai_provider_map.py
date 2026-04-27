"""AI provider name → base URL / 風格 對應表。

使用者只填「公司名稱」(provider) + API key + model,後端依下表決定:
- base_url(API endpoint)
- API 風格:openai-compat / anthropic
- /models 端點(用於 fetch 可用模型清單)

新增一個 provider 只要在這裡加一條 entry。如果使用者填的名稱不在表裡,
後端 fall back 為 OpenAI-compat + base_url 用使用者填的(進階自架情境)。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class ProviderSpec:
    base_url: str
    style: str  # 'openai-compat' / 'anthropic'
    models_path: str = "/models"
    auth_header: str = "Authorization"  # 多數是 Bearer
    auth_prefix: str = "Bearer "
    extra_headers: Optional[dict] = None


# 名字大小寫不敏感(都先 lower 再查)
_PROVIDERS: dict[str, ProviderSpec] = {
    "openai":     ProviderSpec("https://api.openai.com/v1", "openai-compat"),
    "anthropic":  ProviderSpec(
        "https://api.anthropic.com/v1",
        "anthropic",
        models_path="/models",
        auth_header="x-api-key",
        auth_prefix="",
        extra_headers={"anthropic-version": "2023-06-01"},
    ),
    "claude":     ProviderSpec(
        "https://api.anthropic.com/v1",
        "anthropic",
        models_path="/models",
        auth_header="x-api-key",
        auth_prefix="",
        extra_headers={"anthropic-version": "2023-06-01"},
    ),
    "deepseek":   ProviderSpec("https://api.deepseek.com/v1", "openai-compat"),
    "groq":       ProviderSpec("https://api.groq.com/openai/v1", "openai-compat"),
    "openrouter": ProviderSpec("https://openrouter.ai/api/v1", "openai-compat"),
    "together":   ProviderSpec("https://api.together.xyz/v1", "openai-compat"),
    "mistral":    ProviderSpec("https://api.mistral.ai/v1", "openai-compat"),
    "xai":        ProviderSpec("https://api.x.ai/v1", "openai-compat"),
    "grok":       ProviderSpec("https://api.x.ai/v1", "openai-compat"),
    "google":     ProviderSpec(
        "https://generativelanguage.googleapis.com/v1beta",
        "openai-compat",  # Google 提供的 OpenAI-compatible endpoint
        models_path="/openai/models",
    ),
    "gemini":     ProviderSpec(
        "https://generativelanguage.googleapis.com/v1beta",
        "openai-compat",
        models_path="/openai/models",
    ),
    "ollama":     ProviderSpec("http://host.docker.internal:11434/v1", "openai-compat"),
    "lmstudio":   ProviderSpec("http://host.docker.internal:1234/v1", "openai-compat"),
}


def resolve(provider_name: str, base_url_override: Optional[str] = None) -> ProviderSpec:
    """根據使用者填的 provider 名稱找對應 spec。

    base_url_override:若使用者進階設定有填,會覆蓋 spec.base_url。
    沒在表裡 → 預設 OpenAI-compat,base_url 用 override(若有)否則 OpenAI 官方。
    """
    key = (provider_name or "").strip().lower()
    spec = _PROVIDERS.get(key)
    if not spec:
        spec = _PROVIDERS["openai"]
    if base_url_override:
        # dataclass 是 frozen,用 dataclass.replace
        from dataclasses import replace
        spec = replace(spec, base_url=base_url_override.rstrip("/"))
    return spec


def known_providers() -> list[str]:
    """前端 datalist 提示用的常見名字(顯示用駝峰命名)。"""
    return [
        "OpenAI", "Anthropic", "DeepSeek", "Groq", "OpenRouter",
        "Together", "Mistral", "xAI", "Google", "Ollama", "LM Studio",
    ]
