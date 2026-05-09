"""Hermes sidecar HTTP client 單元測試。

PR2:不依賴真的 hermes container,用 httpx MockTransport 驗:
- 路徑構造正確(provision / sessions / messages)
- X-Sidecar-Auth header 一定帶(healthz 例外)
- 5xx / 401 / 502 / connect-error 各自對應到正確 exception 子類
- HERMES_ENABLED=False 時 healthcheck 直接回 false
"""
from __future__ import annotations

import json

import httpx
import pytest

from app.services.hermes_client import (
    HermesAcpError,
    HermesAuthFailed,
    HermesBadRequest,
    HermesError,
    HermesHttpClient,
    HermesNotFound,
    HermesUnavailable,
)


def _make_client(transport: httpx.MockTransport, *, auth: str = "test-token") -> HermesHttpClient:
    """建一個 HermesHttpClient 但用注入的 MockTransport 取代真實 HTTP。"""
    c = HermesHttpClient(base_url="http://hermes:7800", auth_token=auth, timeout=5.0)
    # 把預設 client 換掉,讓所有 request 走 mock
    c._client = httpx.AsyncClient(
        base_url="http://hermes:7800",
        timeout=5.0,
        headers={"X-Sidecar-Auth": auth},
        transport=transport,
    )
    return c


# ── healthcheck ───────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_healthcheck_ok() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        # /healthz 不需 auth — supervisor 端有 middleware bypass
        assert request.url.path == "/healthz"
        return httpx.Response(200, json={"status": "ok"})

    c = _make_client(httpx.MockTransport(handler))
    assert await c.healthcheck() is True
    await c.aclose()


@pytest.mark.asyncio
async def test_healthcheck_503_returns_false_not_raise() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"status": "unhealthy"})

    c = _make_client(httpx.MockTransport(handler))
    # healthcheck 是診斷工具,失敗時不該 raise — 只回 False 讓呼叫端決定怎麼降級
    assert await c.healthcheck() is False
    await c.aclose()


@pytest.mark.asyncio
async def test_healthcheck_disabled_via_feature_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.services.hermes_client.settings.HERMES_ENABLED", False)
    # transport 不會被呼叫 — 提早 false
    c = _make_client(httpx.MockTransport(lambda r: httpx.Response(200)))
    assert await c.healthcheck() is False
    await c.aclose()


# ── provision ───────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_provision_sends_full_payload() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"workspace_id": "ws_1", "status": "provisioned"})

    c = _make_client(httpx.MockTransport(handler))
    await c.provision(
        workspace_id="ws_1",
        provider="openai",
        api_key="sk-real-key",
        base_url="https://example.invalid/v1",
        system_prompt="You are friendly.",
    )
    assert captured["path"] == "/admin/users/ws_1/provision"
    assert captured["headers"].get("x-sidecar-auth") == "test-token"
    assert captured["body"] == {
        "provider": "openai",
        "api_key": "sk-real-key",
        "base_url": "https://example.invalid/v1",
        "system_prompt": "You are friendly.",
    }
    await c.aclose()


@pytest.mark.asyncio
async def test_provision_omits_optional_fields() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={})

    c = _make_client(httpx.MockTransport(handler))
    await c.provision(workspace_id="ws_2", provider="anthropic", api_key="sk-ant-x")
    # base_url / system_prompt 不傳就不該出現在 body — 避免 supervisor 把空值寫進 .env
    assert captured["body"] == {"provider": "anthropic", "api_key": "sk-ant-x"}
    await c.aclose()


# ── sessions / messages ────────────────────────────────────────────
@pytest.mark.asyncio
async def test_create_session_returns_dict() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/workspaces/ws_1/sessions"
        return httpx.Response(200, json={"session_id": "s1", "models": {"x": 1}})

    c = _make_client(httpx.MockTransport(handler))
    result = await c.create_session("ws_1")
    assert result["session_id"] == "s1"
    assert result["models"] == {"x": 1}
    await c.aclose()


@pytest.mark.asyncio
async def test_send_message_url_and_body() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "session_id": "s1",
            "content": "hello back",
            "stop_reason": "end_turn",
            "usage": None,
        })

    c = _make_client(httpx.MockTransport(handler))
    result = await c.send_message("ws_1", "s1", "hello")
    assert captured["path"] == "/v1/workspaces/ws_1/sessions/s1/messages"
    assert captured["body"] == {"content": "hello"}
    assert result["content"] == "hello back"
    assert result["stop_reason"] == "end_turn"
    await c.aclose()


# ── error mapping ──────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_401_maps_to_auth_failed() -> None:
    c = _make_client(httpx.MockTransport(
        lambda r: httpx.Response(401, json={"error": "unauthorized"})
    ))
    with pytest.raises(HermesAuthFailed):
        await c.create_session("ws_1")
    await c.aclose()


@pytest.mark.asyncio
async def test_400_maps_to_bad_request() -> None:
    c = _make_client(httpx.MockTransport(
        lambda r: httpx.Response(400, text="provider_required")
    ))
    with pytest.raises(HermesBadRequest):
        await c.provision("ws_1", "openai", "k")
    await c.aclose()


@pytest.mark.asyncio
async def test_502_maps_to_acp_error_with_code_and_detail() -> None:
    c = _make_client(httpx.MockTransport(lambda r: httpx.Response(
        502, json={"error": "acp_error", "code": -32601, "detail": "Method not found"},
    )))
    with pytest.raises(HermesAcpError) as exc:
        await c.send_message("ws_1", "s1", "hi")
    assert exc.value.code == -32601
    assert "Method not found" in exc.value.detail
    await c.aclose()


@pytest.mark.asyncio
async def test_500_maps_to_unavailable() -> None:
    c = _make_client(httpx.MockTransport(
        lambda r: httpx.Response(500, text="internal")
    ))
    with pytest.raises(HermesUnavailable):
        await c.list_sessions("ws_1")
    await c.aclose()


@pytest.mark.asyncio
async def test_504_maps_to_unavailable() -> None:
    """Sidecar 內部 RPC timeout 時回 504 — 跟連不上 sidecar 同類,都是暫時性。"""
    c = _make_client(httpx.MockTransport(
        lambda r: httpx.Response(504, json={"error": "rpc_timeout"})
    ))
    with pytest.raises(HermesUnavailable):
        await c.send_message("ws_1", "s1", "hi")
    await c.aclose()


@pytest.mark.asyncio
async def test_connect_error_maps_to_unavailable() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    c = _make_client(httpx.MockTransport(handler))
    # 業務路徑(create_session)應 raise — 讓上層 router 決定回 503
    with pytest.raises(HermesUnavailable):
        await c.create_session("ws_1")
    # 但 healthcheck() 是診斷工具,連不上就回 False(不 raise),
    # 讓上層 graceful degradation 邏輯能直接判斷
    assert await c.healthcheck() is False
    await c.aclose()


# ── PR4: list_skills + search_memory ─────────────────────────────────
@pytest.mark.asyncio
async def test_list_skills_url_and_response() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        return httpx.Response(200, json={
            "skills": [
                {"name": "x", "namespace": "y", "description": "",
                 "platforms": [], "path": "y/x/"},
            ]
        })

    c = _make_client(httpx.MockTransport(handler))
    result = await c.list_skills("ws_1")
    assert captured["path"] == "/v1/workspaces/ws_1/skills"
    assert result["skills"][0]["name"] == "x"
    await c.aclose()


@pytest.mark.asyncio
async def test_search_memory_passes_query_and_limit_as_querystring() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        # httpx params 落在 url.query
        captured["query"] = dict(request.url.params)
        return httpx.Response(200, json={
            "results": [],
            "query": "flaky",
            "sanitized_query": "flaky",
            "limit": 7,
        })

    c = _make_client(httpx.MockTransport(handler))
    result = await c.search_memory("ws_1", "flaky", limit=7)
    assert captured["path"] == "/v1/workspaces/ws_1/memory/search"
    assert captured["query"] == {"q": "flaky", "limit": "7"}
    assert result["sanitized_query"] == "flaky"
    await c.aclose()


@pytest.mark.asyncio
async def test_search_memory_sidecar_500_maps_to_unavailable() -> None:
    """state.db locked 之類的 sidecar 端錯誤應該回 5xx;client 統一翻成
    HermesUnavailable 讓 router 用 503 + retry_after 回前端。"""
    c = _make_client(httpx.MockTransport(
        lambda r: httpx.Response(500, text="db locked"),
    ))
    with pytest.raises(HermesUnavailable):
        await c.search_memory("ws_1", "anything")
    await c.aclose()


# ── PR5: cron ────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_list_cron_jobs_url_and_response() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["method"] = request.method
        return httpx.Response(200, json={"jobs": [{"id": "j1", "name": "t"}]})

    c = _make_client(httpx.MockTransport(handler))
    result = await c.list_cron_jobs("ws_1")
    assert captured["path"] == "/v1/workspaces/ws_1/cron"
    assert captured["method"] == "GET"
    assert result["jobs"][0]["id"] == "j1"
    await c.aclose()


@pytest.mark.asyncio
async def test_add_cron_job_sends_full_body() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content)
        return httpx.Response(201, json={"id": "j2", "name": "morning"})

    c = _make_client(httpx.MockTransport(handler))
    result = await c.add_cron_job(
        "ws_1", schedule="0 9 * * *", prompt="run regression", name="morning",
    )
    assert captured["path"] == "/v1/workspaces/ws_1/cron"
    assert captured["body"] == {
        "schedule": "0 9 * * *",
        "prompt": "run regression",
        "name": "morning",
    }
    assert result["id"] == "j2"
    await c.aclose()


@pytest.mark.asyncio
async def test_add_cron_job_omits_optional_name() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(201, json={"id": "j3"})

    c = _make_client(httpx.MockTransport(handler))
    await c.add_cron_job("ws_1", schedule="*/5 * * * *", prompt="x")
    # name 不傳就不該出現在 body — 對齊既有 provision pattern
    assert captured["body"] == {"schedule": "*/5 * * * *", "prompt": "x"}
    await c.aclose()


@pytest.mark.asyncio
async def test_delete_cron_job_returns_none() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["method"] = request.method
        return httpx.Response(204)

    c = _make_client(httpx.MockTransport(handler))
    result = await c.delete_cron_job("ws_1", "j1")
    assert result is None
    assert captured["path"] == "/v1/workspaces/ws_1/cron/j1"
    assert captured["method"] == "DELETE"
    await c.aclose()


@pytest.mark.asyncio
async def test_delete_cron_404_maps_to_not_found() -> None:
    """sidecar 回 404 必須翻成 HermesNotFound,讓 router 能轉 404 給前端
    (不是泛 HermesError 也不是 5xx)。"""
    c = _make_client(httpx.MockTransport(
        lambda r: httpx.Response(404, text="job_not_found"),
    ))
    with pytest.raises(HermesNotFound):
        await c.delete_cron_job("ws_1", "no_such_job")
    await c.aclose()


# ── Gateway PR ──────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_gateway_status_url() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["method"] = request.method
        return httpx.Response(200, json={
            "platforms": {"telegram": {"enabled": True, "has_token": True, "extra": {}}},
            "daemon": {"running": True, "uptime_sec": 1.0,
                       "last_exit_code": None, "recent_stderr": []},
        })

    c = _make_client(httpx.MockTransport(handler))
    result = await c.gateway_status("ws_1")
    assert captured["path"] == "/v1/workspaces/ws_1/gateway"
    assert captured["method"] == "GET"
    assert result["platforms"]["telegram"]["has_token"] is True
    await c.aclose()


@pytest.mark.asyncio
async def test_gateway_enable_sends_token_and_extra() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "platform": "telegram", "enabled": True,
            "daemon": {"running": True, "uptime_sec": 0.1,
                       "last_exit_code": None, "recent_stderr": []},
        })

    c = _make_client(httpx.MockTransport(handler))
    await c.gateway_enable(
        "ws_1", "telegram", token="bot_tok_123", extra={"allow_all": False},
    )
    assert captured["path"] == "/v1/workspaces/ws_1/gateway/telegram/enable"
    assert captured["body"] == {
        "token": "bot_tok_123",
        "extra": {"allow_all": False},
    }
    await c.aclose()


@pytest.mark.asyncio
async def test_gateway_enable_omits_extra_when_none() -> None:
    """extra=None 時不該出現在 body — 跟 add_cron_job(name=None)同 pattern。"""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "platform": "telegram", "enabled": True,
            "daemon": {"running": True, "uptime_sec": 0.0,
                       "last_exit_code": None, "recent_stderr": []},
        })

    c = _make_client(httpx.MockTransport(handler))
    await c.gateway_enable("ws_1", "telegram", token="x")
    assert captured["body"] == {"token": "x"}
    await c.aclose()


@pytest.mark.asyncio
async def test_gateway_disable_url_and_no_body() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["method"] = request.method
        captured["content"] = request.content
        return httpx.Response(204)

    c = _make_client(httpx.MockTransport(handler))
    result = await c.gateway_disable("ws_1", "telegram")
    assert result is None
    assert captured["path"] == "/v1/workspaces/ws_1/gateway/telegram/disable"
    assert captured["method"] == "POST"
    assert captured["content"] == b""  # 沒 body
    await c.aclose()


@pytest.mark.asyncio
async def test_gateway_enable_502_maps_to_acp_error() -> None:
    """Sidecar daemon 啟動失敗時回 502;client 翻成 HermesAcpError 讓 router 502。"""
    c = _make_client(httpx.MockTransport(lambda r: httpx.Response(
        502, json={"error": "daemon_start_failed", "code": -32099,
                   "detail": "telegram-bot library not installed"},
    )))
    with pytest.raises(HermesAcpError) as exc:
        await c.gateway_enable("ws_1", "telegram", token="x")
    assert "telegram-bot" in exc.value.detail
    await c.aclose()


@pytest.mark.asyncio
async def test_unexpected_status_maps_to_generic_hermes_error() -> None:
    """4xx 但不是 400/401 — 不該 silently 通過,當作協定 bug。"""
    c = _make_client(httpx.MockTransport(
        lambda r: httpx.Response(418, text="i am a teapot")
    ))
    with pytest.raises(HermesError) as exc:
        await c.create_session("ws_1")
    # 不該被歸類成 unavailable / acp / auth / bad — 是 raw HermesError 提示協定不對
    assert not isinstance(exc.value, (HermesUnavailable, HermesAcpError,
                                      HermesAuthFailed, HermesBadRequest))
    await c.aclose()
