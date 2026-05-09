"""mem0 sidecar client unit tests。

PR3:用 httpx MockTransport 跑 contract test,驗:
- URL / payload / header(X-Sidecar-Auth)正確
- 各 status code 對應到正確 exception 子類
- Circuit breaker 連續 5 次失敗 trip,cooldown 內 fast-fail
- *_safe 系列在 mem0 故障時不 raise
- MEM0_ENABLED=False 時 *_safe 直接回 default(避免無謂 HTTP call)
"""
from __future__ import annotations

import json

import httpx
import pytest

from app.services.mem0_client import (
    Mem0AuthFailed,
    Mem0BadRequest,
    Mem0Client,
    Mem0Error,
    Mem0NotFound,
    Mem0ProviderError,
    Mem0Unavailable,
)


LLM = {"provider": "openai", "config": {"api_key": "sk-fake", "model": "gpt-4o-mini"}}
EMB = {"provider": "openai", "config": {"api_key": "sk-fake", "model": "text-embedding-3-small"}}


def _make_client(transport: httpx.MockTransport, *, auth: str = "test-tok") -> Mem0Client:
    c = Mem0Client(base_url="http://mem0:7900", auth_token=auth, timeout=5.0, search_timeout=3.0)
    c._client = httpx.AsyncClient(
        base_url="http://mem0:7900",
        timeout=5.0,
        headers={"X-Sidecar-Auth": auth},
        transport=transport,
    )
    return c


# ── healthcheck ─────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_healthcheck_ok() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/healthz"
        return httpx.Response(200, json={"status": "ok"})
    c = _make_client(httpx.MockTransport(handler))
    assert await c.healthcheck() is True
    await c.aclose()


@pytest.mark.asyncio
async def test_healthcheck_503_returns_false_not_raise() -> None:
    c = _make_client(httpx.MockTransport(
        lambda r: httpx.Response(503, json={"status": "unhealthy"})
    ))
    assert await c.healthcheck() is False
    await c.aclose()


@pytest.mark.asyncio
async def test_healthcheck_disabled_via_feature_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.services.mem0_client.settings.MEM0_ENABLED", False)
    c = _make_client(httpx.MockTransport(lambda r: httpx.Response(200)))
    assert await c.healthcheck() is False
    await c.aclose()


# ── add ──────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_add_sends_payload() -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["path"] = req.url.path
        captured["headers"] = dict(req.headers)
        captured["body"] = json.loads(req.content)
        return httpx.Response(200, json={"status": "ok"})

    c = _make_client(httpx.MockTransport(handler))
    await c.add(
        user_id="alice",
        messages=[{"role": "user", "content": "hi"}],
        llm_config=LLM,
        embedder_config=EMB,
        metadata={"session_id": "s1"},
    )
    assert captured["path"] == "/v1/memory/add"
    assert captured["headers"]["x-sidecar-auth"] == "test-tok"
    body = captured["body"]
    assert body["user_id"] == "alice"
    assert body["llm_config"] == LLM
    assert body["embedder_config"] == EMB
    assert body["metadata"] == {"session_id": "s1"}
    assert body["infer"] is True
    await c.aclose()


# ── search / list / delete ─────────────────────────────────────────
@pytest.mark.asyncio
async def test_search_returns_results_list_only() -> None:
    """sidecar 回 {results: [...]},client 透傳該 list 而非整個 dict。"""
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": [{"id": "f1", "memory": "..."}, {"id": "f2"}]})
    c = _make_client(httpx.MockTransport(handler))
    out = await c.search("alice", "test", LLM, EMB, top_k=5)
    assert isinstance(out, list)
    assert len(out) == 2
    assert out[0]["id"] == "f1"
    await c.aclose()


@pytest.mark.asyncio
async def test_search_empty_results() -> None:
    c = _make_client(httpx.MockTransport(lambda r: httpx.Response(200, json={"results": []})))
    assert await c.search("alice", "x", LLM, EMB) == []
    await c.aclose()


@pytest.mark.asyncio
async def test_delete_memory_url() -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["path"] = req.url.path
        captured["method"] = req.method
        captured["body"] = json.loads(req.content)
        return httpx.Response(204)

    c = _make_client(httpx.MockTransport(handler))
    await c.delete_memory("alice", "fact-1", LLM, EMB)
    assert captured["path"] == "/v1/memory/fact-1"
    assert captured["method"] == "DELETE"
    assert captured["body"]["user_id"] == "alice"
    await c.aclose()


@pytest.mark.asyncio
async def test_delete_all_includes_confirm_true() -> None:
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(req.content)
        return httpx.Response(200, json={"status": "ok"})

    c = _make_client(httpx.MockTransport(handler))
    await c.delete_all("alice", LLM, EMB)
    assert captured["body"]["confirm"] is True
    assert captured["body"]["user_id"] == "alice"
    await c.aclose()


# ── error mapping ───────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_401_maps_to_auth_failed() -> None:
    c = _make_client(httpx.MockTransport(
        lambda r: httpx.Response(401, json={"error": "unauthorized"})
    ))
    with pytest.raises(Mem0AuthFailed):
        await c.search("alice", "x", LLM, EMB)
    await c.aclose()


@pytest.mark.asyncio
async def test_400_maps_to_bad_request() -> None:
    c = _make_client(httpx.MockTransport(
        lambda r: httpx.Response(400, json={"detail": "user_id required"})
    ))
    with pytest.raises(Mem0BadRequest):
        await c.search("alice", "x", LLM, EMB)
    await c.aclose()


@pytest.mark.asyncio
async def test_404_maps_to_not_found() -> None:
    c = _make_client(httpx.MockTransport(
        lambda r: httpx.Response(404, text="memory_not_found")
    ))
    with pytest.raises(Mem0NotFound):
        await c.delete_memory("alice", "no-such", LLM, EMB)
    await c.aclose()


@pytest.mark.asyncio
async def test_502_maps_to_provider_error() -> None:
    """sidecar 回 502 = mem0 lib 內 LLM provider 拒絕(invalid key、quota)。"""
    c = _make_client(httpx.MockTransport(
        lambda r: httpx.Response(502, json={"detail": "mem0_add_failed: AuthenticationError: 401"})
    ))
    with pytest.raises(Mem0ProviderError) as exc:
        await c.add("alice", [], LLM, EMB)
    assert "AuthenticationError" in str(exc.value)
    await c.aclose()


@pytest.mark.asyncio
async def test_500_maps_to_unavailable() -> None:
    c = _make_client(httpx.MockTransport(
        lambda r: httpx.Response(500, text="internal error")
    ))
    with pytest.raises(Mem0Unavailable):
        await c.search("alice", "x", LLM, EMB)
    await c.aclose()


@pytest.mark.asyncio
async def test_connect_error_maps_to_unavailable() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")
    c = _make_client(httpx.MockTransport(handler))
    with pytest.raises(Mem0Unavailable):
        await c.search("alice", "x", LLM, EMB)
    await c.aclose()


# ── Circuit breaker ─────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_circuit_breaker_trips_after_5_failures() -> None:
    """連續 5 個 5xx 後 breaker open,後續 call fast-fail 不打 HTTP。"""
    call_count = {"n": 0}

    def handler(_req: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return httpx.Response(500)

    c = _make_client(httpx.MockTransport(handler))
    for _ in range(5):
        with pytest.raises(Mem0Unavailable):
            await c.search("alice", "x", LLM, EMB)
    # 第 6 次:breaker open,直接 raise 不再打 HTTP
    with pytest.raises(Mem0Unavailable, match="circuit_breaker_open"):
        await c.search("alice", "x", LLM, EMB)
    assert call_count["n"] == 5  # 第 6 次沒打到 handler
    await c.aclose()


@pytest.mark.asyncio
async def test_circuit_breaker_resets_after_success() -> None:
    """1 次失敗後成功 — counter 歸零。"""
    states = ["fail", "ok"]

    def handler(_req: httpx.Request) -> httpx.Response:
        s = states.pop(0)
        if s == "fail":
            return httpx.Response(500)
        return httpx.Response(200, json={"results": []})

    c = _make_client(httpx.MockTransport(handler))
    with pytest.raises(Mem0Unavailable):
        await c.search("alice", "x", LLM, EMB)
    # 第 2 次成功
    out = await c.search("alice", "x", LLM, EMB)
    assert out == []
    # 應該 reset(內部驗 _fail_count == 0)
    assert c._breaker._fail_count == 0
    await c.aclose()


# ── _safe wrappers(graceful degrade)────────────────────────────────
@pytest.mark.asyncio
async def test_add_safe_returns_false_on_error_does_not_raise() -> None:
    c = _make_client(httpx.MockTransport(
        lambda r: httpx.Response(502, json={"detail": "LLM error"})
    ))
    result = await c.add_safe(user_id="alice", messages=[], llm_config=LLM, embedder_config=EMB)
    assert result is False  # 沒 raise,只回 False
    await c.aclose()


@pytest.mark.asyncio
async def test_add_safe_returns_true_on_success() -> None:
    c = _make_client(httpx.MockTransport(lambda r: httpx.Response(200, json={"status": "ok"})))
    assert await c.add_safe("alice", [], LLM, EMB) is True
    await c.aclose()


@pytest.mark.asyncio
async def test_add_safe_disabled_returns_false_no_call(monkeypatch) -> None:
    monkeypatch.setattr("app.services.mem0_client.settings.MEM0_ENABLED", False)
    call_count = {"n": 0}

    def handler(_r):
        call_count["n"] += 1
        return httpx.Response(200)

    c = _make_client(httpx.MockTransport(handler))
    assert await c.add_safe("alice", [], LLM, EMB) is False
    assert call_count["n"] == 0  # MEM0_ENABLED=False 直接 short-circuit
    await c.aclose()


@pytest.mark.asyncio
async def test_search_safe_returns_empty_on_error() -> None:
    c = _make_client(httpx.MockTransport(
        lambda r: httpx.Response(500, text="boom")
    ))
    assert await c.search_safe("alice", "x", LLM, EMB) == []
    await c.aclose()


@pytest.mark.asyncio
async def test_search_safe_disabled_returns_empty(monkeypatch) -> None:
    monkeypatch.setattr("app.services.mem0_client.settings.MEM0_ENABLED", False)
    c = _make_client(httpx.MockTransport(lambda r: httpx.Response(200)))
    assert await c.search_safe("alice", "x", LLM, EMB) == []
    await c.aclose()
