"""LlmProviderConfig + llm_config_service 單元測試。

只跑 unit-level(不開真實 DB):
1. Fernet round-trip 走 EncryptedString TypeDecorator(直接呼叫 process_bind/result)
2. Schema 把空字串 api_key 視為「不動」(EmailConfig 慣例)
3. resolve_provider 優先級:org-row > global default > env
4. Response schema 永不洩漏 api_key 明碼

resolve_provider 用 in-memory fake AsyncSession,避免 testcontainer 依賴。
完整 endpoint level 的測試會在 integration test 用真 Postgres 驗。
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

import pytest

from app.auth.crypto import EncryptedString, decrypt_str, encrypt_str
from app.llm.providers import AnthropicProvider, OpenAIProvider
from app.models.llm_provider_config import LlmProviderConfig
from app.schemas.llm_provider import (
    LlmProviderConfigUpdate,
    LlmProviderTestRequest,
)
from app.services import llm_config_service


# ── Fernet round-trip ────────────────────────────────────────────────


def test_encrypted_string_round_trip_via_typedecorator() -> None:
    """EncryptedString.process_bind_param 加密;process_result_value 解密。"""
    col = EncryptedString(500)
    plain = "sk-ant-12345abcdef"
    stored = col.process_bind_param(plain, dialect=None)
    assert stored is not None
    assert stored.startswith("fernet:")
    assert plain not in stored  # 明碼絕不能出現

    recovered = col.process_result_value(stored, dialect=None)
    assert recovered == plain


def test_encrypted_string_handles_none_and_empty() -> None:
    col = EncryptedString(500)
    assert col.process_bind_param(None, dialect=None) is None
    assert col.process_result_value(None, dialect=None) is None
    # empty string 仍是 empty(crypto.py 規格)
    assert col.process_bind_param("", dialect=None) == ""


def test_encrypt_str_idempotent_does_not_double_encrypt() -> None:
    once = encrypt_str("sk-test")
    twice = encrypt_str(once)
    assert once == twice
    assert decrypt_str(once) == "sk-test"


# ── Schema:空 api_key 視為不動 ────────────────────────────────────


def test_schema_empty_api_key_means_no_change() -> None:
    """payload.api_key 是 None / 空字串時,service 不該動既有 row.api_key。"""
    p = LlmProviderConfigUpdate(provider="anthropic", api_key="", enabled=True)
    assert p.api_key == ""
    p2 = LlmProviderConfigUpdate(provider="anthropic", api_key=None, enabled=True)
    assert p2.api_key is None


def test_schema_strips_blank_base_url() -> None:
    p = LlmProviderConfigUpdate(provider="openai", base_url="   ", enabled=True)
    assert p.base_url is None


# ── resolve_provider 優先級 ─────────────────────────────────────────


def _make_row(
    *,
    provider: str,
    org_id: Optional[str],
    api_key: str,
    enabled: bool = True,
) -> LlmProviderConfig:
    row = LlmProviderConfig()
    row.id = f"{org_id or 'global'}-{provider}"
    row.organization_id = org_id
    row.provider = provider
    # 注意:這裡是測 service 邏輯,直接 set 明文(ORM TypeDecorator 不會觸發)
    row.api_key = api_key
    row.base_url = None
    row.default_model = None
    row.enabled = enabled
    row.created_at = datetime(2026, 5, 28)
    row.updated_at = datetime(2026, 5, 28)
    return row


_DB_SENTINEL = object()  # 不會被 service 觸碰,因為 get_for_org 被 monkeypatch


@pytest.fixture
def patch_get_for_org(monkeypatch):
    """提供一個小工廠,讓每個測試指定 (org_id, provider) → row 對應關係。"""

    def _install(table: dict[tuple[Optional[str], str], LlmProviderConfig]):
        async def fake_get(db, organization_id, provider):
            return table.get((organization_id, provider))

        monkeypatch.setattr(llm_config_service, "get_for_org", fake_get)

    return _install


@pytest.mark.asyncio
async def test_resolve_provider_prefers_org_row_over_global(patch_get_for_org) -> None:
    org_row = _make_row(provider="anthropic", org_id="org-A", api_key="key-A")
    global_row = _make_row(provider="anthropic", org_id=None, api_key="key-GLOBAL")
    patch_get_for_org({("org-A", "anthropic"): org_row, (None, "anthropic"): global_row})

    p = await llm_config_service.resolve_provider(
        _DB_SENTINEL, provider_name="anthropic", organization_id="org-A"
    )
    assert isinstance(p, AnthropicProvider)
    assert p._api_key == "key-A"  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_resolve_provider_falls_back_to_global_when_org_row_missing(
    patch_get_for_org,
) -> None:
    global_row = _make_row(provider="openai", org_id=None, api_key="key-GLOBAL")
    patch_get_for_org({(None, "openai"): global_row})

    p = await llm_config_service.resolve_provider(
        _DB_SENTINEL, provider_name="openai", organization_id="org-X"
    )
    assert isinstance(p, OpenAIProvider)
    assert p._api_key == "key-GLOBAL"  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_resolve_provider_skips_disabled_row(patch_get_for_org) -> None:
    org_row = _make_row(
        provider="google", org_id="org-A", api_key="key-A", enabled=False
    )
    patch_get_for_org({("org-A", "google"): org_row})
    # 沒 global、env 也沒設 → 應 raise
    from app import config as _cfg

    saved = _cfg.settings.GOOGLE_API_KEY
    _cfg.settings.GOOGLE_API_KEY = ""
    try:
        with pytest.raises(ValueError, match="Google"):
            await llm_config_service.resolve_provider(
                _DB_SENTINEL, provider_name="google", organization_id="org-A"
            )
    finally:
        _cfg.settings.GOOGLE_API_KEY = saved


@pytest.mark.asyncio
async def test_resolve_provider_falls_back_to_env_when_no_db_row(
    patch_get_for_org,
) -> None:
    patch_get_for_org({})
    from app import config as _cfg

    saved = _cfg.settings.ANTHROPIC_API_KEY
    _cfg.settings.ANTHROPIC_API_KEY = "sk-from-env"
    try:
        p = await llm_config_service.resolve_provider(
            _DB_SENTINEL, provider_name="anthropic", organization_id="org-A"
        )
        assert isinstance(p, AnthropicProvider)
        assert p._api_key == "sk-from-env"  # type: ignore[attr-defined]
    finally:
        _cfg.settings.ANTHROPIC_API_KEY = saved


@pytest.mark.asyncio
async def test_resolve_provider_rejects_unknown_provider(patch_get_for_org) -> None:
    patch_get_for_org({})
    with pytest.raises(ValueError, match="未知 provider"):
        await llm_config_service.resolve_provider(
            _DB_SENTINEL, provider_name="cohere", organization_id="org-A"
        )


@pytest.mark.asyncio
async def test_resolve_provider_openai_with_custom_base_url(patch_get_for_org) -> None:
    row = _make_row(provider="openai", org_id="org-A", api_key="key-X")
    row.base_url = "http://ollama:11434/v1/chat/completions"
    patch_get_for_org({("org-A", "openai"): row})

    p = await llm_config_service.resolve_provider(
        _DB_SENTINEL, provider_name="openai", organization_id="org-A"
    )
    assert isinstance(p, OpenAIProvider)
    assert p._url == "http://ollama:11434/v1/chat/completions"  # type: ignore[attr-defined]


# ── Response schema 不洩漏 api_key ──────────────────────────────────


def test_response_schema_does_not_include_api_key_field() -> None:
    """LlmProviderConfigResponse 的 model fields 不能有 ``api_key``;
    只能有 ``has_api_key`` bool 與 ``key_prefix`` 遮罩字串。"""
    from app.schemas.llm_provider import LlmProviderConfigResponse

    fields = set(LlmProviderConfigResponse.model_fields.keys())
    assert "api_key" not in fields
    assert "has_api_key" in fields
    assert "key_prefix" in fields


def test_test_request_defaults_model_and_prompt_optional() -> None:
    req = LlmProviderTestRequest()
    assert req.model is None
    assert req.prompt is None


# ── key_prefix 計算 ────────────────────────────────────────────────


def test_compute_key_prefix_long_key_shows_head_and_tail() -> None:
    out = llm_config_service._compute_key_prefix("sk-ant-api03-aBcDe123456XyZw")
    # 前 8 + *** + 末 4
    assert out == "sk-ant-a***XyZw"


def test_compute_key_prefix_short_key_only_shows_head() -> None:
    assert llm_config_service._compute_key_prefix("sk-shorty") == "sk-s***"
    assert llm_config_service._compute_key_prefix("abc") == "abc***"


def test_compute_key_prefix_empty_returns_empty() -> None:
    assert llm_config_service._compute_key_prefix("") == ""
    assert llm_config_service._compute_key_prefix("   ") == ""


def test_compute_key_prefix_never_leaks_middle() -> None:
    """確保中間 secret 段不會出現在 prefix 字串裡。"""
    secret = "MIDDLESECRETPART"
    raw = f"sk-ant-{secret}-TAIL"
    out = llm_config_service._compute_key_prefix(raw)
    assert secret not in out


# ── record_usage 寫入行為 ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_record_usage_constructs_row_from_chat_result() -> None:
    """驗證 ChatResult → AgentTokenUsage row 欄位對應正確,Decimal 轉換無浮點誤差。"""
    from app.llm.base import ChatResult, Usage
    from app.services import llm_usage_service

    class _CaptureDB:
        def __init__(self):
            self.added = None

        def add(self, row):
            self.added = row

        async def flush(self):
            return None

    db = _CaptureDB()
    cr = ChatResult(
        content_text="hi",
        tool_calls=[],
        usage=Usage(
            input_tokens=100,
            output_tokens=50,
            cache_read_tokens=80,
            cache_write_tokens=20,
            cost_usd=0.001234,
        ),
        model="claude-opus-4-7",
        provider="anthropic",
        stop_reason="end_turn",
        raw_response_id="msg_xyz",
    )
    row = await llm_usage_service.record_usage(
        db,  # type: ignore[arg-type]
        organization_id="org-A",
        user_id="user-B",
        session_id="sess-C",
        result=cr,
    )
    assert row is not None
    assert row.organization_id == "org-A"
    assert row.user_id == "user-B"
    assert row.session_id == "sess-C"
    assert row.provider == "anthropic"
    assert row.model == "claude-opus-4-7"
    assert row.input_tokens == 100
    assert row.output_tokens == 50
    assert row.cache_read_tokens == 80
    assert row.cache_write_tokens == 20
    assert str(row.cost_usd) == "0.001234"
    assert row.stop_reason == "end_turn"
    assert row.response_id == "msg_xyz"


@pytest.mark.asyncio
async def test_record_usage_swallows_db_error_and_returns_none() -> None:
    """寫入失敗時不該 raise,否則整段 chat 結果會被丟掉。"""
    from app.llm.base import ChatResult, Usage
    from app.services import llm_usage_service

    class _BrokenDB:
        def add(self, row):
            raise RuntimeError("db is on fire")

        async def flush(self):
            return None

    cr = ChatResult(
        content_text="",
        tool_calls=[],
        usage=Usage(input_tokens=1, output_tokens=1, cost_usd=0.0),
        model="gpt-4o-mini",
        provider="openai",
        stop_reason="end_turn",
    )
    out = await llm_usage_service.record_usage(
        _BrokenDB(),  # type: ignore[arg-type]
        organization_id=None,
        user_id=None,
        session_id=None,
        result=cr,
    )
    assert out is None  # 失敗回 None,不 raise
