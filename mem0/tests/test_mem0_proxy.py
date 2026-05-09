"""mem0 sidecar proxy tests。

PR2 範圍:用 FastAPI TestClient + monkeypatch `Memory.from_config` 取代真實 mem0
lib(避免 test 跑時連 mem0-postgres + LLM API),verify proxy 自身行為:
- Auth gate(X-Sidecar-Auth)
- user_id 必填、不接受 client 自帶 metadata.user_id
- delete 跨 user ownership check 回 404
- delete_all 沒 confirm 拒絕
- LRU cache hit:同 config 不重複建 Memory
"""
from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock

# 必要 env(import mem0_proxy 時會 sys.exit 若缺)
os.environ.setdefault("MEM0_SIDECAR_AUTH_TOKEN", "test-auth-token")
os.environ.setdefault("MEM0_PG_PASSWORD", "test-pg-pass")

# 讓 import mem0_proxy 找得到(放在 mem0/ 上層)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient

import mem0_proxy

AUTH_HEADERS = {"X-Sidecar-Auth": "test-auth-token"}
LLM = {"provider": "openai", "config": {"api_key": "sk-fake", "model": "gpt-4o-mini"}}
EMB = {"provider": "openai", "config": {"api_key": "sk-fake", "model": "text-embedding-3-small"}}


@pytest.fixture
def client(monkeypatch):
    """Mock _get_memory 回一個 MagicMock,避免真連 pgvector + LLM。"""
    fake_memory = MagicMock()
    fake_memory.add.return_value = {"results": [{"id": "fact-1", "memory": "user prefers Pytest"}]}
    fake_memory.search.return_value = {
        "results": [{"id": "fact-1", "memory": "user prefers Pytest", "score": 0.95}]
    }
    fake_memory.get_all.return_value = {
        "results": [{"id": "fact-1", "memory": "user prefers Pytest"}]
    }
    fake_memory.get.return_value = {"id": "fact-1", "user_id": "alice", "memory": "..."}
    fake_memory.delete.return_value = None
    fake_memory.delete_all.return_value = None

    # _get_memory 是 module-level fn,monkeypatch 整個替換 — 同一 user 拿到同一 MagicMock
    monkeypatch.setattr(mem0_proxy, "_get_memory", lambda *a, **kw: fake_memory)
    # _ping_pg 也 mock 成 ok,避免 healthz 連真的 pg
    monkeypatch.setattr(mem0_proxy, "_ping_pg", lambda: (True, None))

    with TestClient(mem0_proxy.app) as c:
        c.fake_memory = fake_memory  # 暴露給 test 用
        yield c


# ── /healthz ────────────────────────────────────────────────────────
def test_healthz_no_auth_required(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    # phase string 是內部 metadata,不是契約 — 接受任何 non-empty 值
    assert body["phase"]


def test_healthz_pg_down_returns_503(client, monkeypatch):
    monkeypatch.setattr(mem0_proxy, "_ping_pg", lambda: (False, "pg_unreachable: ..."))
    resp = client.get("/healthz")
    assert resp.status_code == 503
    assert resp.json()["status"] == "unhealthy"


# ── Auth ────────────────────────────────────────────────────────────
def test_no_auth_returns_401(client):
    """non-/healthz 無 X-Sidecar-Auth → 401。"""
    for method, path, body in [
        ("post", "/v1/memory/add", {"user_id": "x", "messages": [], "llm_config": LLM, "embedder_config": EMB}),
        ("post", "/v1/memory/search", {"user_id": "x", "query": "y", "llm_config": LLM, "embedder_config": EMB}),
        ("post", "/v1/memory/list", {"user_id": "x", "llm_config": LLM, "embedder_config": EMB}),
    ]:
        resp = client.request(method, path, json=body)
        assert resp.status_code == 401, f"{method} {path} expected 401"


def test_wrong_auth_returns_401(client):
    resp = client.post(
        "/v1/memory/list",
        json={"user_id": "x", "llm_config": LLM, "embedder_config": EMB},
        headers={"X-Sidecar-Auth": "WRONG"},
    )
    assert resp.status_code == 401


# ── add ──────────────────────────────────────────────────────────────
def test_add_happy_path(client):
    resp = client.post(
        "/v1/memory/add",
        json={
            "user_id": "alice",
            "messages": [{"role": "user", "content": "I prefer Pytest"}],
            "llm_config": LLM,
            "embedder_config": EMB,
            "metadata": {"session_id": "s1"},
        },
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "ok"
    # mem0.add 真的被呼叫,user_id=alice 強制注入
    client.fake_memory.add.assert_called_once()
    call_kwargs = client.fake_memory.add.call_args.kwargs
    assert call_kwargs["user_id"] == "alice"
    assert call_kwargs["metadata"] == {"session_id": "s1"}


def test_add_strips_client_supplied_user_id(client):
    """client 自帶 metadata.user_id 偽造身份 — proxy 必須擦掉。"""
    resp = client.post(
        "/v1/memory/add",
        json={
            "user_id": "alice",
            "messages": [{"role": "user", "content": "..."}],
            "llm_config": LLM, "embedder_config": EMB,
            "metadata": {"user_id": "EVIL_BOB", "session_id": "s1", "agent_id": "x"},
        },
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 200
    call_kwargs = client.fake_memory.add.call_args.kwargs
    # user_id 走 mem0 lib 的 user_id 參數,不會接受 metadata 自帶
    assert call_kwargs["user_id"] == "alice"
    # metadata 內 user_id / agent_id / run_id 全被 strip
    assert "user_id" not in (call_kwargs["metadata"] or {})
    assert "agent_id" not in (call_kwargs["metadata"] or {})
    assert "session_id" in call_kwargs["metadata"]


def test_add_missing_user_id_returns_422(client):
    resp = client.post(
        "/v1/memory/add",
        json={"messages": [], "llm_config": LLM, "embedder_config": EMB},
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 422


def test_add_empty_user_id_returns_422(client):
    resp = client.post(
        "/v1/memory/add",
        json={"user_id": "", "messages": [], "llm_config": LLM, "embedder_config": EMB},
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 422


# ── search ───────────────────────────────────────────────────────────
def test_search_happy_path(client):
    resp = client.post(
        "/v1/memory/search",
        json={
            "user_id": "alice", "query": "test framework",
            "llm_config": LLM, "embedder_config": EMB, "top_k": 3,
        },
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["results"][0]["id"] == "fact-1"
    call_kwargs = client.fake_memory.search.call_args.kwargs
    assert call_kwargs["user_id"] == "alice"
    assert call_kwargs["limit"] == 3


def test_search_empty_query_returns_422(client):
    resp = client.post(
        "/v1/memory/search",
        json={"user_id": "alice", "query": "", "llm_config": LLM, "embedder_config": EMB},
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 422


# ── list ─────────────────────────────────────────────────────────────
def test_list_happy_path(client):
    resp = client.post(
        "/v1/memory/list",
        json={"user_id": "alice", "llm_config": LLM, "embedder_config": EMB, "limit": 20},
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 200
    assert resp.json()["results"][0]["id"] == "fact-1"
    call_kwargs = client.fake_memory.get_all.call_args.kwargs
    assert call_kwargs["user_id"] == "alice"
    assert call_kwargs["limit"] == 20


def test_list_limit_validation(client):
    # 0 / 999 都應 422(Pydantic Field ge/le)
    for bad in [0, 999]:
        resp = client.post(
            "/v1/memory/list",
            json={"user_id": "alice", "llm_config": LLM, "embedder_config": EMB, "limit": bad},
            headers=AUTH_HEADERS,
        )
        assert resp.status_code == 422


# ── delete ───────────────────────────────────────────────────────────
def test_delete_happy_path(client):
    # fake_memory.get 回 user_id=alice,所以 alice 刪自己的 OK
    resp = client.request(
        "DELETE", "/v1/memory/fact-1",
        json={"user_id": "alice", "llm_config": LLM, "embedder_config": EMB},
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 204
    client.fake_memory.delete.assert_called_once_with("fact-1")


def test_delete_cross_user_returns_404(client):
    """B 嘗試刪 A 的 fact — 應 404(不 leak 存在性 vs 沒權限差別)。"""
    # fake_memory.get 預設回 user_id=alice;Bob 來刪
    resp = client.request(
        "DELETE", "/v1/memory/fact-1",
        json={"user_id": "bob", "llm_config": LLM, "embedder_config": EMB},
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 404
    # delete 沒被呼叫(ownership check 在前面就擋了)
    client.fake_memory.delete.assert_not_called()


def test_delete_invalid_memory_id_returns_400(client):
    for bad in ["..%2Fetc", "../etc", "a/b", "x" * 100]:
        resp = client.request(
            "DELETE", f"/v1/memory/{bad}",
            json={"user_id": "alice", "llm_config": LLM, "embedder_config": EMB},
            headers=AUTH_HEADERS,
        )
        # FastAPI 不一定 url-decode,但 .. 與 / 與 length 三層擋
        # 實測:`..%2Fetc` 在 URL 路徑上會被 fastapi decode 成 `../etc`,符合擋線
        assert resp.status_code in (400, 404), f"id={bad} got {resp.status_code}"


def test_delete_metadata_user_id_match(client, monkeypatch):
    """fact 用 metadata.user_id 而非 top-level user_id 也能驗 ownership。"""
    fake = MagicMock()
    fake.get.return_value = {"id": "fact-2", "metadata": {"user_id": "alice"}, "memory": "..."}
    monkeypatch.setattr(mem0_proxy, "_get_memory", lambda *a, **kw: fake)
    resp = client.request(
        "DELETE", "/v1/memory/fact-2",
        json={"user_id": "alice", "llm_config": LLM, "embedder_config": EMB},
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 204


# ── delete_all ───────────────────────────────────────────────────────
def test_delete_all_without_confirm_returns_400(client):
    resp = client.request(
        "DELETE", "/v1/memory/all",
        json={"user_id": "alice", "llm_config": LLM, "embedder_config": EMB, "confirm": False},
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 400


def test_delete_all_with_confirm(client):
    resp = client.request(
        "DELETE", "/v1/memory/all",
        json={"user_id": "alice", "llm_config": LLM, "embedder_config": EMB, "confirm": True},
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 200
    assert resp.json()["deleted_user_id"] == "alice"
    client.fake_memory.delete_all.assert_called_once_with(user_id="alice")


# ── LRU cache(獨立 test,不用 mocked _get_memory)────────────────────
def test_memory_cache_hit_returns_same_object():
    cache = mem0_proxy.MemoryCache(max_size=5, ttl=300)
    factory_calls = {"n": 0}

    def factory():
        factory_calls["n"] += 1
        return MagicMock()

    obj1 = cache.get_or_create("k1", factory)
    obj2 = cache.get_or_create("k1", factory)
    assert obj1 is obj2
    assert factory_calls["n"] == 1


def test_memory_cache_lru_evicts_oldest():
    cache = mem0_proxy.MemoryCache(max_size=2, ttl=300)
    cache.get_or_create("k1", MagicMock)
    cache.get_or_create("k2", MagicMock)
    # 加第三個應 evict k1
    cache.get_or_create("k3", MagicMock)
    assert cache.stats()["size"] == 2
    # k1 重新 create 計為 miss(factory 又被叫)
    fresh_factory = MagicMock(side_effect=lambda: MagicMock())
    cache.get_or_create("k1", fresh_factory)
    fresh_factory.assert_called_once()


def test_redact_secrets_strips_openai_key():
    """LLM provider 401 訊息常含 plaintext key — proxy 必須擦掉。"""
    inp = "Incorrect API key provided: sk-VERY-SECRET-XYZ123. Find your key at..."
    out = mem0_proxy._redact_secrets(inp)
    assert "VERY-SECRET" not in out
    assert "sk-" not in out
    assert "<redacted>" in out


def test_redact_secrets_strips_bearer_token():
    inp = "Authorization: Bearer abc.def.ghi-VERY-SECRET-789"
    out = mem0_proxy._redact_secrets(inp)
    assert "VERY-SECRET" not in out
    assert "<redacted>" in out


def test_502_detail_does_not_leak_key(client, monkeypatch):
    """模擬 mem0.add raise OpenAI AuthenticationError 內含 plaintext key — 確認 proxy
    回的 502 detail 不含。"""
    fake = MagicMock()
    fake.add.side_effect = Exception(
        "AuthenticationError: 401 - Incorrect API key provided: sk-LEAKED-KEY-12345"
    )
    monkeypatch.setattr(mem0_proxy, "_get_memory", lambda *a, **kw: fake)
    resp = client.post(
        "/v1/memory/add",
        json={
            "user_id": "alice",
            "messages": [{"role": "user", "content": "x"}],
            "llm_config": LLM, "embedder_config": EMB,
        },
        headers=AUTH_HEADERS,
    )
    assert resp.status_code == 502
    body = resp.text
    assert "LEAKED-KEY" not in body
    assert "<redacted>" in body


def test_cache_key_does_not_leak_plaintext():
    """sha256 hash 後不該含 plaintext api_key。"""
    key = mem0_proxy._cache_key(
        "alice",
        {"provider": "openai", "config": {"api_key": "sk-VERY-SECRET-XYZ"}},
        {"provider": "openai", "config": {"api_key": "sk-VERY-SECRET-XYZ"}},
    )
    assert "VERY-SECRET" not in key
    assert "sk-" not in key
    assert len(key) == 64  # sha256 hex
