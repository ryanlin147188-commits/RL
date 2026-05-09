"""mem0_llm_config 單元測試。

驗 build_llm_config / build_embedder_config 對各 provider 的 AiTokenConfig 攤平
出的 mem0 expected dict 結構正確。
"""
from __future__ import annotations

from app.models.ai_token_config import AiTokenConfig
from app.services.mem0_llm_config import build_embedder_config, build_llm_config


def _token(**kwargs) -> AiTokenConfig:
    """建一個未持久化的 AiTokenConfig 物件供測試。"""
    defaults = dict(
        name="t", organization_id="org_x", provider="OpenAI",
        api_key="sk-fake", model=None, base_url=None,
        enabled=True, is_default=True,
    )
    defaults.update(kwargs)
    return AiTokenConfig(**defaults)


# ── build_llm_config ─────────────────────────────────────────────────
def test_openai_token_returns_openai_provider() -> None:
    cfg = build_llm_config(_token(provider="OpenAI", model="gpt-4o-mini"))
    assert cfg["provider"] == "openai"
    assert cfg["config"]["api_key"] == "sk-fake"
    assert cfg["config"]["model"] == "gpt-4o-mini"
    # OpenAI 官方不需要 base_url override
    assert cfg["config"].get("openai_base_url", "").endswith("api.openai.com/v1")


def test_anthropic_token_returns_anthropic_provider() -> None:
    cfg = build_llm_config(_token(provider="Anthropic", api_key="sk-ant-x"))
    assert cfg["provider"] == "anthropic"
    assert cfg["config"]["api_key"] == "sk-ant-x"
    assert "claude" in cfg["config"]["model"]


def test_anthropic_default_model_when_unset() -> None:
    cfg = build_llm_config(_token(provider="Anthropic", model=None))
    assert cfg["config"]["model"]  # 不該空


def test_openrouter_token_routes_to_openai_with_base_url() -> None:
    """OpenRouter 是 OpenAI-compat,走 'openai' provider + openai_base_url override。"""
    cfg = build_llm_config(_token(provider="OpenRouter", model="anthropic/claude-opus-4.7"))
    assert cfg["provider"] == "openai"
    assert "openrouter.ai" in cfg["config"]["openai_base_url"]


def test_deepseek_uses_native_provider() -> None:
    """mem0 lib 認識 deepseek native provider — 直接用。"""
    cfg = build_llm_config(_token(provider="DeepSeek", model="deepseek-chat"))
    assert cfg["provider"] == "deepseek"


def test_gemini_token_returns_gemini_provider() -> None:
    cfg = build_llm_config(_token(provider="Gemini", api_key="ga-x"))
    assert cfg["provider"] == "gemini"
    assert cfg["config"]["api_key"] == "ga-x"


def test_unknown_provider_falls_back_to_openai() -> None:
    """完全沒見過的 provider — 走 OpenAI fallback,base_url override 用 spec default。"""
    cfg = build_llm_config(_token(provider="MysteryAI", base_url="https://my.local/v1"))
    assert cfg["provider"] == "openai"
    assert cfg["config"]["openai_base_url"] == "https://my.local/v1"


def test_custom_base_url_override() -> None:
    """ai_token_configs.base_url 進階自架時覆寫 spec 的 base_url。"""
    cfg = build_llm_config(_token(
        provider="OpenAI", base_url="https://my-proxy.foo/v1",
    ))
    # OpenAI spec 預設就是 openai-compat,base_url 被 override
    assert cfg["config"]["openai_base_url"] == "https://my-proxy.foo/v1"


# ── build_embedder_config ────────────────────────────────────────────
def test_openai_token_has_embedder() -> None:
    cfg = build_embedder_config(_token(provider="OpenAI"))
    assert cfg is not None
    assert cfg["provider"] == "openai"
    assert cfg["config"]["model"] == "text-embedding-3-small"
    assert cfg["config"]["embedding_dims"] == 1536


def test_anthropic_token_returns_none_no_embedder() -> None:
    """Anthropic 沒 embedder API — 必須回 None,讓上游降級。"""
    cfg = build_embedder_config(_token(provider="Anthropic"))
    assert cfg is None


def test_openrouter_falls_back_to_openai_embedder_with_base_url() -> None:
    """OpenRouter 的 embedder 端點實際可能不可用,但 config 仍合法輸出 — 由
    sidecar / mem0 lib 在實際 search 時拒絕(graceful degradation)。"""
    cfg = build_embedder_config(_token(provider="OpenRouter"))
    assert cfg is not None
    assert cfg["provider"] == "openai"
    assert "openrouter.ai" in cfg["config"]["openai_base_url"]


def test_gemini_token_has_embedder() -> None:
    cfg = build_embedder_config(_token(provider="Gemini"))
    assert cfg is not None
    assert cfg["provider"] == "gemini"
    assert cfg["config"]["api_key"] == "sk-fake"
    assert cfg["config"]["embedding_dims"] == 1536


def test_unknown_openai_compat_provider_returns_openai_embedder() -> None:
    """完全未知 provider — fallback 到 OpenAI embedder(會在實際 call 時 fail,
    但 config dict 合法)。"""
    cfg = build_embedder_config(_token(provider="MysteryAI", base_url="https://x.local/v1"))
    assert cfg is not None
    assert cfg["provider"] == "openai"


def test_provider_name_case_insensitive() -> None:
    """ai_token_configs.provider 是自由字串,'OpenAI' / 'openai' / 'OPENAI' 都認得。"""
    for raw in ["openai", "OPENAI", "OpenAI"]:
        cfg = build_llm_config(_token(provider=raw))
        assert cfg["provider"] == "openai"
