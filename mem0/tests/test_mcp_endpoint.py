"""mem0 sidecar PR1(Hermes MCP)admin endpoints + search_memory tool tests。

PR1 範圍只測:
- admin POST/DELETE /admin/users/{id}/llm_config(寫/清 cache)
- search_memory tool 直接呼叫(透過 Python 而非 HTTP — FastMCP 的 streamable HTTP
  transport 用 TestClient 不好 mock,改測 tool function 自身邏輯)
- contextvar 注入(模擬 ASGI middleware 行為)
"""
from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock

os.environ.setdefault("MEM0_SIDECAR_AUTH_TOKEN", "test-auth-token")
os.environ.setdefault("MEM0_PG_PASSWORD", "test-pg-pass")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient

import mem0_proxy

AUTH_HEADERS = {"X-Sidecar-Auth": "test-auth-token"}
LLM = {"provider": "openai", "config": {"api_key": "sk-fake", "model": "gpt-4o-mini"}}
EMB = {"provider": "openai", "config": {"api_key": "sk-fake", "model": "text-embedding-3-small"}}


@pytest.fixture(autouse=True)
def reset_caches():
    """每個 test 開頭都把 cache 清乾淨。"""
    mem0_proxy._llm_config_cache._d.clear()
    yield
    mem0_proxy._llm_config_cache._d.clear()


@pytest.fixture
def client():
    with TestClient(mem0_proxy.app) as c:
        yield c


# ── admin endpoints ────────────────────────────────────────────────
def test_admin_push_llm_config(client):
    resp = client.post(
        "/admin/users/org_x:alice/llm_config",
        json={"llm_config": LLM, "embedder_config": EMB},
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ok"
    assert body["user_id"] == "org_x:alice"
    cached = mem0_proxy._llm_config_cache.get("org_x:alice")
    assert cached is not None
    assert cached[0] == LLM
    assert cached[1] == EMB


def test_admin_clear_llm_config(client):
    # 先 push
    client.post("/admin/users/u1/llm_config",
                json={"llm_config": LLM, "embedder_config": EMB},
                headers=AUTH_HEADERS)
    assert mem0_proxy._llm_config_cache.get("u1") is not None
    # 然後 delete
    resp = client.delete("/admin/users/u1/llm_config", headers=AUTH_HEADERS)
    assert resp.status_code == 204
    assert mem0_proxy._llm_config_cache.get("u1") is None


def test_admin_no_auth_returns_401(client):
    resp = client.post(
        "/admin/users/x/llm_config",
        json={"llm_config": LLM, "embedder_config": EMB},
    )
    assert resp.status_code == 401


def test_admin_invalid_user_id_returns_400(client):
    for bad in ["", "a/b", "..", "with space"]:
        resp = client.post(
            f"/admin/users/{bad}/llm_config" if bad else "/admin/users//llm_config",
            json={"llm_config": LLM, "embedder_config": EMB},
            headers=AUTH_HEADERS,
        )
        # FastAPI URL routing 對空 path / 斜線可能 404 — 接受 400/404 都算擋下
        assert resp.status_code in (400, 404), f"bad={bad!r} got {resp.status_code}"


def test_admin_clear_idempotent_for_missing_user(client):
    """沒 cache 的 user delete 仍 204(idempotent)。"""
    resp = client.delete("/admin/users/never-existed/llm_config", headers=AUTH_HEADERS)
    assert resp.status_code == 204


# ── LlmConfigCache TTL ──────────────────────────────────────────────
def test_llm_config_cache_ttl_expiry():
    cache = mem0_proxy.LlmConfigCache(ttl=0)  # 立即過期
    cache.put("u1", LLM, EMB)
    # ttl=0 + monotonic time 過 0 秒 → 已過期
    import time
    time.sleep(0.01)
    assert cache.get("u1") is None


def test_llm_config_cache_returns_value_within_ttl():
    cache = mem0_proxy.LlmConfigCache(ttl=300)
    cache.put("u2", LLM, EMB)
    out = cache.get("u2")
    assert out == (LLM, EMB)


# ── search_memory tool ──────────────────────────────────────────────
@pytest.mark.asyncio
async def test_search_memory_no_user_id_returns_friendly_text():
    """contextvar 沒設(模擬不該發生的 race)→ 回 friendly text 不 raise。"""
    # _current_mcp_user_id 預設 None
    out = await mem0_proxy.search_memory(query="testing", top_k=5)
    assert "Memory unavailable" in out
    assert "user context missing" in out.lower()


@pytest.mark.asyncio
async def test_search_memory_no_cached_config_returns_friendly_text():
    """cache miss(backend 沒 push)→ 回 friendly,別 raise。"""
    token = mem0_proxy._current_mcp_user_id.set("u1")
    try:
        out = await mem0_proxy.search_memory(query="testing", top_k=5)
        assert "temporarily unavailable" in out.lower()
    finally:
        mem0_proxy._current_mcp_user_id.reset(token)


@pytest.mark.asyncio
async def test_search_memory_happy_path(monkeypatch):
    """正常路徑:有 user_id contextvar + 有 cache config → call mem0.search。"""
    fake = MagicMock()
    fake.search.return_value = {
        "results": [
            {"id": "f1", "memory": "User prefers Pytest",
             "created_at": "2026-04-15T10:00:00"},
            {"id": "f2", "memory": "CI uses GitHub Actions",
             "created_at": "2026-04-22T08:00:00"},
        ]
    }
    monkeypatch.setattr(mem0_proxy, "_get_memory", lambda *a, **kw: fake)
    mem0_proxy._llm_config_cache.put("u_alice", LLM, EMB)

    token = mem0_proxy._current_mcp_user_id.set("u_alice")
    try:
        out = await mem0_proxy.search_memory(
            query="testing framework", top_k=5,
        )
    finally:
        mem0_proxy._current_mcp_user_id.reset(token)

    assert "Pytest" in out
    assert "GitHub Actions" in out
    assert "2 memories" in out
    assert "(recorded 2026-04-15)" in out  # 日期 truncate 對齊 plan §1.3

    # mem0.search 真的被呼叫,user_id 是 u_alice
    call = fake.search.call_args
    assert call.kwargs["user_id"] == "u_alice"
    assert call.kwargs["limit"] == 5


@pytest.mark.asyncio
async def test_search_memory_no_matches(monkeypatch):
    fake = MagicMock()
    fake.search.return_value = {"results": []}
    monkeypatch.setattr(mem0_proxy, "_get_memory", lambda *a, **kw: fake)
    mem0_proxy._llm_config_cache.put("u1", LLM, EMB)

    token = mem0_proxy._current_mcp_user_id.set("u1")
    try:
        out = await mem0_proxy.search_memory(query="anything", top_k=5)
    finally:
        mem0_proxy._current_mcp_user_id.reset(token)

    assert "No matching memories" in out
    assert "anything" in out


@pytest.mark.asyncio
async def test_search_memory_top_k_clamped(monkeypatch):
    """top_k > 20 應 clamp 到 20;< 1 clamp 到 1。"""
    fake = MagicMock()
    fake.search.return_value = {"results": []}
    monkeypatch.setattr(mem0_proxy, "_get_memory", lambda *a, **kw: fake)
    mem0_proxy._llm_config_cache.put("u1", LLM, EMB)

    token = mem0_proxy._current_mcp_user_id.set("u1")
    try:
        await mem0_proxy.search_memory(query="x", top_k=999)
        assert fake.search.call_args.kwargs["limit"] == 20
        await mem0_proxy.search_memory(query="x", top_k=0)
        assert fake.search.call_args.kwargs["limit"] == 1
    finally:
        mem0_proxy._current_mcp_user_id.reset(token)


@pytest.mark.asyncio
async def test_search_memory_swallows_mem0_exception(monkeypatch):
    """mem0.search raise → 回 friendly text 不 raise。"""
    fake = MagicMock()
    fake.search.side_effect = Exception(
        "AuthenticationError: 401 - Incorrect API key sk-LEAK"
    )
    monkeypatch.setattr(mem0_proxy, "_get_memory", lambda *a, **kw: fake)
    mem0_proxy._llm_config_cache.put("u1", LLM, EMB)

    token = mem0_proxy._current_mcp_user_id.set("u1")
    try:
        out = await mem0_proxy.search_memory(query="anything", top_k=5)
    finally:
        mem0_proxy._current_mcp_user_id.reset(token)

    assert "Failed to search memory" in out
    # token 不該 leak 到 LLM 看的 string(如果走 _redact_secrets)— 但這個訊息
    # 是 friendly text,沒帶 stack;只要確認 "sk-LEAK" 不在 user-facing reply
    assert "sk-LEAK" not in out
