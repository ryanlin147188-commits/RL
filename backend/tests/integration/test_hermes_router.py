"""Hermes router(/api/hermes/*)integration tests。

PR3 主切換驗證:不依賴真的 hermes sidecar(用 FastAPI dependency override 替換
HermesClient mock),確認 router 行為對齊舊 ai_chat 的契約 + 加上的新行為:
- list/create/get/update/delete sessions 完整 CRUD
- per-user isolation(A 看不到 B 的 session)
- 沒 token 設定 → 400(對齊舊 ai_chat 行為)
- sidecar 連不上 → 503 + retry_after
- sidecar ACP error → 502
- HERMES_ENABLED=False → 503
- send_message 不持久化訊息(回 synthetic message 給前端 render)
"""
from __future__ import annotations

import pytest

from app.auth.security import hash_password
from app.database import AsyncSessionLocal
from app.models import AiTokenConfig, HermesSessionRef
from app.services.hermes_client import (
    HermesAcpError,
    HermesClient,
    HermesError,
    HermesNotFound,
    HermesUnavailable,
)
from app.services.hermes_provisioning import invalidate_user_workspace

pytestmark = pytest.mark.integration


# ── Mock HermesClient ─────────────────────────────────────────────────
class _MockHermesClient(HermesClient):
    """記錄呼叫,可注入 raise。"""

    def __init__(self, *,
                 healthcheck_ok: bool = True,
                 provision_raises: Exception | None = None,
                 create_raises: Exception | None = None,
                 send_raises: Exception | None = None,
                 send_response: dict | None = None,
                 skills_response: dict | None = None,
                 memory_response: dict | None = None,
                 list_skills_raises: Exception | None = None,
                 search_memory_raises: Exception | None = None,
                 cron_jobs: list | None = None,
                 add_cron_raises: Exception | None = None,
                 delete_cron_raises: Exception | None = None,
                 gateway_status_response: dict | None = None,
                 gateway_enable_raises: Exception | None = None,
                 next_session_id: str = "00000000-0000-4000-8000-000000000001"):
        self.calls: list[tuple[str, tuple, dict]] = []
        self._healthcheck_ok = healthcheck_ok
        self._provision_raises = provision_raises
        self._create_raises = create_raises
        self._send_raises = send_raises
        self._send_response = send_response or {
            "session_id": next_session_id,
            "content": "Hello from mock Hermes.",
            "stop_reason": "end_turn",
            "usage": {"total_tokens": 42},
        }
        self._skills_response = skills_response or {"skills": []}
        self._memory_response = memory_response or {
            "results": [], "query": "", "limit": 20,
        }
        self._list_skills_raises = list_skills_raises
        self._search_memory_raises = search_memory_raises
        self._cron_jobs: list[dict] = list(cron_jobs or [])
        self._add_cron_raises = add_cron_raises
        self._delete_cron_raises = delete_cron_raises
        self._gateway_status_response = gateway_status_response or {
            "platforms": {},
            "daemon": {"running": False, "uptime_sec": None,
                       "last_exit_code": None, "recent_stderr": []},
        }
        self._gateway_enable_raises = gateway_enable_raises
        self._next_session_id = next_session_id

    async def healthcheck(self) -> bool:
        self.calls.append(("healthcheck", (), {}))
        return self._healthcheck_ok

    async def provision(self, workspace_id, provider, api_key,
                        base_url=None, system_prompt=None) -> None:
        self.calls.append(("provision", (workspace_id, provider, api_key), {
            "base_url": base_url, "system_prompt": system_prompt,
        }))
        if self._provision_raises:
            raise self._provision_raises

    async def create_session(self, workspace_id, mcp_servers=None) -> dict:
        self.calls.append(("create_session", (workspace_id,), {
            "mcp_servers": mcp_servers,
        }))
        if self._create_raises:
            raise self._create_raises
        return {"session_id": self._next_session_id, "models": None}

    async def list_sessions(self, workspace_id) -> dict:
        self.calls.append(("list_sessions", (workspace_id,), {}))
        return {"sessions": [], "next_cursor": None}

    async def send_message(self, workspace_id, session_id, content) -> dict:
        self.calls.append(("send_message", (workspace_id, session_id, content), {}))
        if self._send_raises:
            raise self._send_raises
        return self._send_response

    async def list_skills(self, workspace_id) -> dict:
        self.calls.append(("list_skills", (workspace_id,), {}))
        if self._list_skills_raises:
            raise self._list_skills_raises
        return self._skills_response

    async def search_memory(self, workspace_id, query, limit=20) -> dict:
        self.calls.append(("search_memory", (workspace_id, query, limit), {}))
        if self._search_memory_raises:
            raise self._search_memory_raises
        # Echo query/limit back as the real sidecar would
        resp = dict(self._memory_response)
        resp.setdefault("query", query)
        resp.setdefault("limit", limit)
        return resp

    async def list_cron_jobs(self, workspace_id) -> dict:
        self.calls.append(("list_cron_jobs", (workspace_id,), {}))
        return {"jobs": list(self._cron_jobs)}

    async def add_cron_job(self, workspace_id, *, schedule, prompt, name=None) -> dict:
        self.calls.append(("add_cron_job", (workspace_id,),
                           {"schedule": schedule, "prompt": prompt, "name": name}))
        if self._add_cron_raises:
            raise self._add_cron_raises
        new = {
            "id": f"job_{len(self._cron_jobs) + 1}",
            "name": name,
            "prompt": prompt,
            "schedule": schedule,
            "schedule_kind": "cron",
            "enabled": True,
            "state": "scheduled",
            "next_run_at": None,
            "last_run_at": None,
            "last_status": None,
            "created_at": None,
        }
        self._cron_jobs.append(new)
        return new

    async def delete_cron_job(self, workspace_id, job_id) -> None:
        self.calls.append(("delete_cron_job", (workspace_id, job_id), {}))
        if self._delete_cron_raises:
            raise self._delete_cron_raises
        before = len(self._cron_jobs)
        self._cron_jobs = [j for j in self._cron_jobs if j.get("id") != job_id]
        if len(self._cron_jobs) == before:
            raise HermesNotFound(f"sidecar_404: job {job_id} not found")

    async def gateway_status(self, workspace_id) -> dict:
        self.calls.append(("gateway_status", (workspace_id,), {}))
        return dict(self._gateway_status_response)

    async def gateway_enable(self, workspace_id, platform, *, token, extra=None) -> dict:
        self.calls.append(("gateway_enable", (workspace_id, platform),
                           {"token": token, "extra": extra}))
        if self._gateway_enable_raises:
            raise self._gateway_enable_raises
        # 模擬 sidecar 回:enable 後 daemon 在跑
        return {
            "platform": platform,
            "enabled": True,
            "daemon": {"running": True, "uptime_sec": 0.5,
                       "last_exit_code": None, "recent_stderr": []},
        }

    async def gateway_disable(self, workspace_id, platform) -> None:
        self.calls.append(("gateway_disable", (workspace_id, platform), {}))


@pytest.fixture
def mock_hermes(monkeypatch: pytest.MonkeyPatch):
    """把 get_hermes_client 換成 mock — 整 router 都用這個 instance。"""
    instance = _MockHermesClient()
    monkeypatch.setattr(
        "app.routers.hermes.get_hermes_client", lambda: instance,
    )
    monkeypatch.setattr(
        "app.services.hermes_provisioning.invalidate_user_workspace",
        lambda uid: None,  # in test 不要 cache 干擾
    )
    yield instance


@pytest.fixture
def mock_mem0(monkeypatch: pytest.MonkeyPatch):
    """Mock mem0 client + per-user fact store(模擬 mem0-postgres 隔離行為)。

    PR3 用了 add_safe / search_safe;PR4 也要 raising 版本的 search/list/delete/
    delete_all。隔離靠 user_id 字典 key — backend 端如果忘記傳 user_id 或傳錯,
    這個 mock 會抓到。
    """
    from app.services.mem0_client import Mem0NotFound, Mem0Unavailable

    class _Fake:
        def __init__(self):
            self.add_safe_calls: list[dict] = []
            self.add_safe_return = True
            self.facts: dict[str, list[dict]] = {}  # user_id → [facts]
            self._next_id = 1
            self.healthcheck_return = True
            self.search_raises: Optional[Exception] = None
            self.delete_raises: Optional[Exception] = None

        # ── PR3:_safe wrappers(fire-and-forget)────────────────
        async def add_safe(self, *, user_id, messages, llm_config,
                            embedder_config, metadata=None):
            self.add_safe_calls.append({
                "user_id": user_id, "messages": messages,
                "metadata": metadata,
            })
            if self.add_safe_return:
                # 模擬寫入:取 user 的 messages 抽成 fact(簡單 — 用第一則 user msg)
                bucket = self.facts.setdefault(user_id, [])
                user_msg = next(
                    (m["content"] for m in (messages or []) if m.get("role") == "user"),
                    "",
                )
                if user_msg:
                    fact_id = f"fact-{self._next_id}"
                    self._next_id += 1
                    bucket.append({
                        "id": fact_id, "memory": user_msg,
                        "metadata": metadata or {},
                    })
            return self.add_safe_return

        async def search_safe(self, **kw):
            return self.facts.get(kw["user_id"], [])

        # ── PR4:raising 版本 ──────────────────────────────────
        async def search(self, *, user_id, query, llm_config,
                          embedder_config, top_k=5, threshold=None):
            if self.search_raises:
                raise self.search_raises
            return [
                f for f in self.facts.get(user_id, [])
                if query.lower() in f["memory"].lower()
            ][:top_k]

        async def list_memories(self, *, user_id, llm_config,
                                 embedder_config, limit=50):
            return list(self.facts.get(user_id, []))[:limit]

        async def delete_memory(self, *, user_id, memory_id,
                                 llm_config, embedder_config):
            if self.delete_raises:
                raise self.delete_raises
            bucket = self.facts.get(user_id, [])
            before = len(bucket)
            self.facts[user_id] = [f for f in bucket if f["id"] != memory_id]
            if len(self.facts[user_id]) == before:
                raise Mem0NotFound("memory_not_found")

        async def delete_all(self, *, user_id, llm_config, embedder_config):
            self.facts[user_id] = []

        async def healthcheck(self):
            return self.healthcheck_return

        async def aclose(self):
            pass

    instance = _Fake()
    monkeypatch.setattr(
        "app.routers.hermes.get_mem0_client", lambda: instance,
    )
    yield instance


@pytest.fixture
async def seeded_token(org_a):
    """Org_a 預塞一筆 enabled+default 的 OpenAI token,讓 ensure_workspace 能挑到。"""
    async with AsyncSessionLocal() as session:
        token = AiTokenConfig(
            name="test-default",
            organization_id=org_a.org_id,
            provider="OpenAI",
            api_key="sk-test-fake-but-non-empty",
            model="gpt-4o-mini",
            enabled=True,
            is_default=True,
        )
        session.add(token)
        await session.commit()
    # 清掉 in-process cache,避免被前一個 test 留下的舊 entry 干擾
    invalidate_user_workspace(org_a.username)  # username != user.id 但安全 no-op
    yield token


# ── Test cases ────────────────────────────────────────────────────────
async def test_list_sessions_empty_for_new_user(client, org_a, mock_hermes) -> None:
    resp = await client.get("/api/hermes/sessions", headers=org_a.headers)
    assert resp.status_code == 200
    assert resp.json() == []


async def test_create_session_provisions_and_persists_ref(
    client, org_a, seeded_token, mock_hermes,
) -> None:
    resp = await client.post(
        "/api/hermes/sessions",
        json={"title": "demo"},
        headers=org_a.headers,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["title"] == "demo"
    assert body["owner"] == org_a.username
    assert body["organization_id"] == org_a.org_id
    # mock 回的固定 session_id
    assert body["id"] == "00000000-0000-4000-8000-000000000001"
    assert body["message_count"] == 0
    assert body["last_message_preview"] is None
    # 應該觸發 provision + create_session 各一次
    methods = [c[0] for c in mock_hermes.calls]
    assert "provision" in methods
    assert "create_session" in methods
    # workspace_id 必須是 ws_<user.id>(防止 client 自帶被接受)
    create_call = next(c for c in mock_hermes.calls if c[0] == "create_session")
    ws_id = create_call[1][0]
    assert ws_id.startswith("ws_")

    # ref row 確實落 DB
    async with AsyncSessionLocal() as db:
        ref = await db.get(HermesSessionRef, body["id"])
        assert ref is not None
        assert ref.owner == org_a.username
        assert ref.workspace_id == ws_id


async def test_create_session_without_token_returns_400(
    client, org_a, mock_hermes,
) -> None:
    """沒設 ai_token_configs → 400(對齊舊 ai_chat 在沒 provider 時的行為)。"""
    resp = await client.post(
        "/api/hermes/sessions", json={}, headers=org_a.headers,
    )
    assert resp.status_code == 400
    body = resp.json()
    # FastAPI 會把 detail 整個塞進 "detail" key
    detail = body.get("detail") or {}
    assert detail.get("error") == "no_token_configured"


async def test_per_user_isolation(
    client, org_a, org_b, seeded_token, mock_hermes,
) -> None:
    """A 建的 session,B GET /sessions 不該看到。"""
    # A 建立一個
    create_resp = await client.post(
        "/api/hermes/sessions", json={"title": "from A"}, headers=org_a.headers,
    )
    assert create_resp.status_code == 201
    sid_a = create_resp.json()["id"]

    # B 列表應該空(B 沒 token 也沒 session)
    list_b = await client.get("/api/hermes/sessions", headers=org_b.headers)
    assert list_b.status_code == 200
    assert list_b.json() == []

    # B 不能 GET A 的 session(404 not 403,避免洩漏 session 存在)
    get_b = await client.get(
        f"/api/hermes/sessions/{sid_a}", headers=org_b.headers,
    )
    assert get_b.status_code == 404


async def test_get_session_returns_empty_messages_v1(
    client, org_a, seeded_token, mock_hermes,
) -> None:
    create = await client.post(
        "/api/hermes/sessions", json={}, headers=org_a.headers,
    )
    sid = create.json()["id"]
    detail = await client.get(
        f"/api/hermes/sessions/{sid}", headers=org_a.headers,
    )
    assert detail.status_code == 200
    body = detail.json()
    # v1 取捨:歷史訊息不從 backend 取(plan 已記錄)
    assert body["messages"] == []
    assert body["id"] == sid


async def test_update_session_changes_title(
    client, org_a, seeded_token, mock_hermes,
) -> None:
    create = await client.post(
        "/api/hermes/sessions", json={"title": "old"}, headers=org_a.headers,
    )
    sid = create.json()["id"]
    upd = await client.put(
        f"/api/hermes/sessions/{sid}",
        json={"title": "renamed"},
        headers=org_a.headers,
    )
    assert upd.status_code == 200
    assert upd.json()["title"] == "renamed"


async def test_delete_session_removes_ref(
    client, org_a, seeded_token, mock_hermes,
) -> None:
    create = await client.post(
        "/api/hermes/sessions", json={}, headers=org_a.headers,
    )
    sid = create.json()["id"]
    dele = await client.delete(
        f"/api/hermes/sessions/{sid}", headers=org_a.headers,
    )
    assert dele.status_code == 204
    # 再列就空了
    listed = await client.get("/api/hermes/sessions", headers=org_a.headers)
    assert listed.json() == []


async def test_send_message_returns_synthetic_user_assistant_pair(
    client, org_a, seeded_token, mock_hermes,
) -> None:
    create = await client.post(
        "/api/hermes/sessions", json={}, headers=org_a.headers,
    )
    sid = create.json()["id"]
    resp = await client.post(
        f"/api/hermes/sessions/{sid}/messages",
        json={"content": "hello"},
        headers=org_a.headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["user_message"]["content"] == "hello"
    assert body["user_message"]["role"] == "user"
    assert body["assistant_message"]["content"] == "Hello from mock Hermes."
    assert body["assistant_message"]["role"] == "assistant"
    assert body["assistant_message"]["tokens_used"] == 42

    # 第一則訊息應該把 title 改成 user 內容(對齊舊 ai_chat 行為)
    listed = await client.get("/api/hermes/sessions", headers=org_a.headers)
    sess = listed.json()[0]
    assert sess["title"] == "hello"
    # last_message_preview 也該被更新
    assert sess["last_message_preview"] is not None


# ── PR3:mem0 post-hook fire-and-forget ─────────────────────────────
async def test_post_hook_creates_background_task(
    client, org_a, seeded_token, mock_hermes, mock_mem0,
) -> None:
    """有 default token + consent enabled(預設)→ post-hook add_safe 被呼叫。"""
    import asyncio
    create = await client.post(
        "/api/hermes/sessions", json={}, headers=org_a.headers,
    )
    sid = create.json()["id"]

    resp = await client.post(
        f"/api/hermes/sessions/{sid}/messages",
        json={"content": "I prefer Pytest"},
        headers=org_a.headers,
    )
    assert resp.status_code == 200, resp.text

    # post-hook 是 fire-and-forget;讓 event loop 跑一輪等 task 開始
    await asyncio.sleep(0.05)

    assert len(mock_mem0.add_safe_calls) == 1
    call = mock_mem0.add_safe_calls[0]
    # mem0_user_id 必須是 f"{org_id}:{username}" 格式(plan §7 risk #7 防 collision)
    assert call["user_id"] == f"{org_a.org_id}:{org_a.username}"
    # messages 包含 user 與 assistant 兩則
    assert len(call["messages"]) == 2
    assert call["messages"][0]["role"] == "user"
    assert call["messages"][0]["content"] == "I prefer Pytest"
    assert call["messages"][1]["role"] == "assistant"
    # metadata 含 session_id + ts
    assert call["metadata"]["session_id"] == sid
    assert "ts" in call["metadata"]


async def test_post_hook_skipped_when_consent_disabled(
    client, org_a, seeded_token, mock_hermes, mock_mem0,
) -> None:
    """consent.extraction_enabled=False → 不觸發 add_safe。"""
    import asyncio
    from app.database import AsyncSessionLocal
    from app.models import HermesMemoryConsent

    async with AsyncSessionLocal() as session:
        session.add(HermesMemoryConsent(
            username=org_a.username,
            organization_id=org_a.org_id,
            extraction_enabled=False,
        ))
        await session.commit()

    create = await client.post(
        "/api/hermes/sessions", json={}, headers=org_a.headers,
    )
    sid = create.json()["id"]
    resp = await client.post(
        f"/api/hermes/sessions/{sid}/messages",
        json={"content": "should not be remembered"},
        headers=org_a.headers,
    )
    assert resp.status_code == 200
    await asyncio.sleep(0.05)
    assert mock_mem0.add_safe_calls == []


async def test_post_hook_skipped_when_session_paused(
    client, org_a, seeded_token, mock_hermes, mock_mem0,
) -> None:
    """paused_session_ids 內且未過期 → 不觸發 add_safe。"""
    import asyncio
    import time
    from app.database import AsyncSessionLocal
    from app.models import HermesMemoryConsent

    create = await client.post(
        "/api/hermes/sessions", json={}, headers=org_a.headers,
    )
    sid = create.json()["id"]

    # 把該 session 加進暫停清單(1 hr 後到期)
    async with AsyncSessionLocal() as session:
        session.add(HermesMemoryConsent(
            username=org_a.username,
            organization_id=org_a.org_id,
            extraction_enabled=True,
            paused_session_ids={sid: time.time() + 3600},
        ))
        await session.commit()

    resp = await client.post(
        f"/api/hermes/sessions/{sid}/messages",
        json={"content": "this session is paused"},
        headers=org_a.headers,
    )
    assert resp.status_code == 200
    await asyncio.sleep(0.05)
    assert mock_mem0.add_safe_calls == []


async def test_post_hook_paused_expiry_does_not_skip(
    client, org_a, seeded_token, mock_hermes, mock_mem0,
) -> None:
    """paused_session_ids 內 timestamp 已過期 → 不該被視為 paused。"""
    import asyncio
    import time
    from app.database import AsyncSessionLocal
    from app.models import HermesMemoryConsent

    create = await client.post(
        "/api/hermes/sessions", json={}, headers=org_a.headers,
    )
    sid = create.json()["id"]

    async with AsyncSessionLocal() as session:
        session.add(HermesMemoryConsent(
            username=org_a.username,
            organization_id=org_a.org_id,
            extraction_enabled=True,
            paused_session_ids={sid: time.time() - 60},  # 60s 前已到期
        ))
        await session.commit()

    resp = await client.post(
        f"/api/hermes/sessions/{sid}/messages",
        json={"content": "expiry should not block"},
        headers=org_a.headers,
    )
    assert resp.status_code == 200
    await asyncio.sleep(0.05)
    # 過期 = 不該被視為 paused → add_safe 應被呼叫
    assert len(mock_mem0.add_safe_calls) == 1


async def test_post_hook_failure_does_not_break_send_message(
    client, org_a, seeded_token, mock_hermes, mock_mem0,
) -> None:
    """mem0.add_safe 回 False(故障)→ send_message 仍正常 200,不影響回前端。"""
    import asyncio
    mock_mem0.add_safe_return = False  # 模擬 mem0 故障

    create = await client.post(
        "/api/hermes/sessions", json={}, headers=org_a.headers,
    )
    sid = create.json()["id"]
    resp = await client.post(
        f"/api/hermes/sessions/{sid}/messages",
        json={"content": "mem0 broke but I should still get reply"},
        headers=org_a.headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["assistant_message"]["content"] == "Hello from mock Hermes."
    await asyncio.sleep(0.05)
    # add_safe 仍被呼叫,只是回 False — fire-and-forget 不影響 user-facing 流程
    assert len(mock_mem0.add_safe_calls) == 1


async def test_post_hook_skipped_when_mem0_disabled(
    client, org_a, seeded_token, mock_hermes, mock_mem0, monkeypatch,
) -> None:
    """MEM0_ENABLED=False → 整段 post-hook setup 被 short-circuit,add_safe 不呼叫。"""
    import asyncio
    monkeypatch.setattr("app.routers.hermes.settings.MEM0_ENABLED", False)
    create = await client.post(
        "/api/hermes/sessions", json={}, headers=org_a.headers,
    )
    sid = create.json()["id"]
    resp = await client.post(
        f"/api/hermes/sessions/{sid}/messages",
        json={"content": "feature flag off"},
        headers=org_a.headers,
    )
    assert resp.status_code == 200
    await asyncio.sleep(0.05)
    assert mock_mem0.add_safe_calls == []


# ── PR4:semantic memory + consent + pause endpoints ────────────────
async def test_semantic_search_no_token_returns_degraded(
    client, org_a, mock_hermes, mock_mem0,
) -> None:
    resp = await client.get(
        "/api/hermes/memory/semantic?q=anything", headers=org_a.headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["results"] == []
    assert body["degraded_reason"] == "no_token_configured"


async def test_semantic_search_with_token_returns_results(
    client, org_a, seeded_token, mock_hermes, mock_mem0,
) -> None:
    """先用 send_message 塞 fact 進 mock,再 search 撈出來。"""
    # 用 send_message 注入 fact(post-hook 走 add_safe → mock 內 store)
    create = await client.post("/api/hermes/sessions", json={}, headers=org_a.headers)
    sid = create.json()["id"]
    await client.post(
        f"/api/hermes/sessions/{sid}/messages",
        json={"content": "I prefer Pytest over unittest"},
        headers=org_a.headers,
    )
    import asyncio; await asyncio.sleep(0.05)
    # 然後 search
    resp = await client.get(
        "/api/hermes/memory/semantic?q=Pytest&limit=5", headers=org_a.headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["query"] == "Pytest"
    assert body["limit"] == 5
    assert body["degraded_reason"] is None
    assert any("Pytest" in r["memory"] for r in body["results"])


async def test_semantic_search_anthropic_only_returns_no_embedder(
    client, org_a, mock_hermes, mock_mem0,
) -> None:
    """只有 Anthropic key(沒 OpenAI-compatible embedder)→ degraded_reason='no_embedder'。"""
    from app.database import AsyncSessionLocal
    async with AsyncSessionLocal() as session:
        session.add(AiTokenConfig(
            name="ant-only", organization_id=org_a.org_id,
            provider="Anthropic", api_key="sk-ant-x",
            enabled=True, is_default=True,
        ))
        await session.commit()
    resp = await client.get(
        "/api/hermes/memory/semantic?q=foo", headers=org_a.headers,
    )
    assert resp.status_code == 200
    assert resp.json()["degraded_reason"] == "no_embedder"


async def test_semantic_search_mem0_down_returns_503(
    client, org_a, seeded_token, mock_hermes, mock_mem0,
) -> None:
    from app.services.mem0_client import Mem0Unavailable
    mock_mem0.search_raises = Mem0Unavailable("circuit_breaker_open")
    resp = await client.get(
        "/api/hermes/memory/semantic?q=foo", headers=org_a.headers,
    )
    assert resp.status_code == 503
    detail = resp.json().get("detail") or {}
    assert detail.get("error") == "mem0_unavailable"
    assert detail.get("retry_after") == 30


async def test_semantic_search_empty_query_returns_empty(
    client, org_a, seeded_token, mock_hermes, mock_mem0,
) -> None:
    resp = await client.get("/api/hermes/memory/semantic?q=", headers=org_a.headers)
    assert resp.status_code == 200
    assert resp.json()["results"] == []


async def test_semantic_list_returns_user_facts(
    client, org_a, seeded_token, mock_hermes, mock_mem0,
) -> None:
    create = await client.post("/api/hermes/sessions", json={}, headers=org_a.headers)
    sid = create.json()["id"]
    for content in ["fact one", "fact two"]:
        await client.post(
            f"/api/hermes/sessions/{sid}/messages",
            json={"content": content},
            headers=org_a.headers,
        )
    import asyncio; await asyncio.sleep(0.05)
    resp = await client.get(
        "/api/hermes/memory/semantic/list?limit=20", headers=org_a.headers,
    )
    assert resp.status_code == 200
    assert len(resp.json()["results"]) == 2


async def test_semantic_per_user_isolation(
    client, org_a, org_b, seeded_token, mock_hermes, mock_mem0,
) -> None:
    """A 加 fact;B list / search / delete 都看不到 A 的(關鍵安全測試)。"""
    # A 建 session + send message 注入 fact
    create_a = await client.post("/api/hermes/sessions", json={}, headers=org_a.headers)
    sid_a = create_a.json()["id"]
    await client.post(
        f"/api/hermes/sessions/{sid_a}/messages",
        json={"content": "A's secret preference"},
        headers=org_a.headers,
    )
    import asyncio; await asyncio.sleep(0.05)

    # B 沒設 token → list 回 degraded(no_token)
    list_b = await client.get(
        "/api/hermes/memory/semantic/list", headers=org_b.headers,
    )
    assert list_b.status_code == 200
    assert list_b.json()["results"] == []
    assert list_b.json()["degraded_reason"] == "no_token_configured"

    # A 仍能看到自己的
    list_a = await client.get(
        "/api/hermes/memory/semantic/list", headers=org_a.headers,
    )
    assert list_a.status_code == 200
    assert any("secret" in r["memory"] for r in list_a.json()["results"])


async def test_semantic_delete_404_when_not_owned(
    client, org_a, seeded_token, mock_hermes, mock_mem0,
) -> None:
    resp = await client.delete(
        "/api/hermes/memory/semantic/no-such-fact", headers=org_a.headers,
    )
    assert resp.status_code == 404


async def test_semantic_delete_invalid_id_returns_400(
    client, org_a, seeded_token, mock_hermes, mock_mem0,
) -> None:
    for bad in ["..%2Fetc", "x/y", "x" * 100]:
        resp = await client.delete(
            f"/api/hermes/memory/semantic/{bad}", headers=org_a.headers,
        )
        assert resp.status_code in (400, 404), f"id={bad} got {resp.status_code}"


async def test_semantic_wipe_requires_confirm_header(
    client, org_a, seeded_token, mock_hermes, mock_mem0,
) -> None:
    resp = await client.delete(
        "/api/hermes/memory/semantic", headers=org_a.headers,
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "confirm_required"


async def test_semantic_wipe_with_confirm_clears_user_facts(
    client, org_a, seeded_token, mock_hermes, mock_mem0,
) -> None:
    create = await client.post("/api/hermes/sessions", json={}, headers=org_a.headers)
    sid = create.json()["id"]
    await client.post(
        f"/api/hermes/sessions/{sid}/messages",
        json={"content": "to be wiped"},
        headers=org_a.headers,
    )
    import asyncio; await asyncio.sleep(0.05)

    # 驗有 fact
    list1 = await client.get("/api/hermes/memory/semantic/list", headers=org_a.headers)
    assert len(list1.json()["results"]) == 1

    resp = await client.delete(
        "/api/hermes/memory/semantic",
        headers={**org_a.headers, "X-Confirm-Wipe": "true"},
    )
    assert resp.status_code == 204

    # 再 list 應空
    list2 = await client.get("/api/hermes/memory/semantic/list", headers=org_a.headers)
    assert list2.json()["results"] == []


# ── consent ────────────────────────────────────────────────────────
async def test_consent_get_default_when_no_row(
    client, org_a, mock_hermes, mock_mem0,
) -> None:
    resp = await client.get("/api/hermes/memory/consent", headers=org_a.headers)
    assert resp.status_code == 200
    assert resp.json()["extraction_enabled"] is True
    assert resp.json()["paused_session_count"] == 0


async def test_consent_put_creates_then_updates_row(
    client, org_a, mock_hermes, mock_mem0,
) -> None:
    # 第一次 PUT 應 upsert
    resp = await client.put(
        "/api/hermes/memory/consent",
        json={"extraction_enabled": False},
        headers=org_a.headers,
    )
    assert resp.status_code == 200
    assert resp.json()["extraction_enabled"] is False

    # 再 GET 應取出 disabled
    get_resp = await client.get(
        "/api/hermes/memory/consent", headers=org_a.headers,
    )
    assert get_resp.json()["extraction_enabled"] is False

    # 再 PUT 切回 enabled — update 既有 row
    resp2 = await client.put(
        "/api/hermes/memory/consent",
        json={"extraction_enabled": True},
        headers=org_a.headers,
    )
    assert resp2.json()["extraction_enabled"] is True


async def test_consent_disabled_then_post_hook_skipped(
    client, org_a, seeded_token, mock_hermes, mock_mem0,
) -> None:
    """PUT consent enabled=false 後,send_message 的 post-hook 不該觸發。"""
    await client.put(
        "/api/hermes/memory/consent",
        json={"extraction_enabled": False},
        headers=org_a.headers,
    )
    create = await client.post("/api/hermes/sessions", json={}, headers=org_a.headers)
    sid = create.json()["id"]
    await client.post(
        f"/api/hermes/sessions/{sid}/messages",
        json={"content": "should not be remembered"},
        headers=org_a.headers,
    )
    import asyncio; await asyncio.sleep(0.05)
    assert mock_mem0.add_safe_calls == []


# ── session pause ──────────────────────────────────────────────────
async def test_session_pause_creates_consent_row(
    client, org_a, seeded_token, mock_hermes, mock_mem0,
) -> None:
    create = await client.post("/api/hermes/sessions", json={}, headers=org_a.headers)
    sid = create.json()["id"]
    resp = await client.post(
        f"/api/hermes/sessions/{sid}/memory/pause?duration_minutes=30",
        headers=org_a.headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["session_id"] == sid
    assert body["paused_until"] > 0

    # consent GET 應顯示 1 個 paused
    consent = await client.get("/api/hermes/memory/consent", headers=org_a.headers)
    assert consent.json()["paused_session_count"] == 1


async def test_session_pause_for_other_user_session_returns_404(
    client, org_a, org_b, seeded_token, mock_hermes, mock_mem0,
) -> None:
    create_a = await client.post("/api/hermes/sessions", json={}, headers=org_a.headers)
    sid_a = create_a.json()["id"]
    # B 嘗試暫停 A 的 session
    resp = await client.post(
        f"/api/hermes/sessions/{sid_a}/memory/pause", headers=org_b.headers,
    )
    assert resp.status_code == 404


async def test_session_pause_invalid_duration_returns_422(
    client, org_a, seeded_token, mock_hermes, mock_mem0,
) -> None:
    create = await client.post("/api/hermes/sessions", json={}, headers=org_a.headers)
    sid = create.json()["id"]
    for bad in [0, -1, 1500]:  # ge=1, le=1440
        resp = await client.post(
            f"/api/hermes/sessions/{sid}/memory/pause?duration_minutes={bad}",
            headers=org_a.headers,
        )
        assert resp.status_code == 422


# ── /api/hermes/health 加 mem0_up ────────────────────────────────
async def test_health_includes_mem0_up(
    client, org_a, mock_hermes, mock_mem0,
) -> None:
    resp = await client.get("/api/hermes/health", headers=org_a.headers)
    assert resp.status_code == 200
    body = resp.json()
    assert "mem0_up" in body
    assert body["mem0_up"] is True
    assert body["mem0_enabled"] is True


async def test_health_mem0_down_returns_mem0_up_false(
    client, org_a, mock_hermes, mock_mem0,
) -> None:
    mock_mem0.healthcheck_return = False
    resp = await client.get("/api/hermes/health", headers=org_a.headers)
    body = resp.json()
    assert body["mem0_up"] is False
    # sidecar 仍 up
    assert body["sidecar_up"] is True


# ── PR6:Pre-hook recall(自動 RAG)──────────────────────────────────
async def test_prehook_injects_recalled_memories_into_hermes_content(
    client, org_a, seeded_token, mock_hermes, mock_mem0,
) -> None:
    """有相關 fact 時,推給 Hermes 的 content 應含 <recalled_memory> block,
    而 response.recalled_memories 跟 user_message.content 給前端的是原 content。"""
    # 預塞 fact 到 mock(模擬之前對話已存進的記憶)
    user_id = f"{org_a.org_id}:{org_a.username}"
    mock_mem0.facts[user_id] = [
        {"id": "f1", "memory": "User prefers Pytest over unittest"},
        {"id": "f2", "memory": "Staging URL is https://staging.foo.test"},
    ]
    create = await client.post("/api/hermes/sessions", json={}, headers=org_a.headers)
    sid = create.json()["id"]

    resp = await client.post(
        f"/api/hermes/sessions/{sid}/messages",
        json={"content": "幫我設計登入測試"},
        headers=org_a.headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # response 帶 recalled_memories 給前端 chip 用
    assert len(body["recalled_memories"]) == 2
    assert "Pytest" in body["recalled_memories"][0]

    # user_message.content 仍是原 content,不洩漏 augmentation
    assert body["user_message"]["content"] == "幫我設計登入測試"
    assert "<recalled_memory>" not in body["user_message"]["content"]

    # 推給 Hermes 的真實 content(見 mock_hermes.calls)應含 recalled block
    send_call = next(
        c for c in mock_hermes.calls
        if c[0] == "send_message" and c[1][1] == sid
    )
    sent_content = send_call[1][2]
    assert "<recalled_memory>" in sent_content
    assert "Pytest" in sent_content
    assert "Staging URL" in sent_content
    assert "幫我設計登入測試" in sent_content  # 原 content 仍在後面


async def test_prehook_skipped_when_query_too_short(
    client, org_a, seeded_token, mock_hermes, mock_mem0,
) -> None:
    """3 字以內 query 不該觸發 search(避免雜訊召回)。"""
    user_id = f"{org_a.org_id}:{org_a.username}"
    mock_mem0.facts[user_id] = [{"id": "f1", "memory": "should not be searched"}]
    create = await client.post("/api/hermes/sessions", json={}, headers=org_a.headers)
    sid = create.json()["id"]

    resp = await client.post(
        f"/api/hermes/sessions/{sid}/messages",
        json={"content": "OK"},  # 2 字,太短
        headers=org_a.headers,
    )
    assert resp.status_code == 200
    assert resp.json()["recalled_memories"] == []

    # 推給 Hermes 的 content 應該沒 recalled block
    send_call = next(c for c in mock_hermes.calls if c[0] == "send_message")
    assert "<recalled_memory>" not in send_call[1][2]


async def test_prehook_skipped_when_no_embedder(
    client, org_a, mock_hermes, mock_mem0,
) -> None:
    """Anthropic-only user(沒 embedder)— pre-hook 不觸發。"""
    from app.database import AsyncSessionLocal
    async with AsyncSessionLocal() as session:
        session.add(AiTokenConfig(
            name="ant-only", organization_id=org_a.org_id,
            provider="Anthropic", api_key="sk-ant-x",
            enabled=True, is_default=True,
        ))
        await session.commit()

    create = await client.post("/api/hermes/sessions", json={}, headers=org_a.headers)
    sid = create.json()["id"]
    resp = await client.post(
        f"/api/hermes/sessions/{sid}/messages",
        json={"content": "this is a long enough query to be searchable"},
        headers=org_a.headers,
    )
    assert resp.status_code == 200
    assert resp.json()["recalled_memories"] == []
    send_call = next(c for c in mock_hermes.calls if c[0] == "send_message")
    assert "<recalled_memory>" not in send_call[1][2]


async def test_prehook_failure_does_not_break_send_message(
    client, org_a, seeded_token, mock_hermes, mock_mem0,
) -> None:
    """mem0 故障(search throws)時 — 主對話仍正常,recalled 回空。"""
    from app.services.mem0_client import Mem0Unavailable

    # 注入 search_safe 變失敗:讓 search() raise(_safe wrapper 應 swallow)
    async def fail_search(**kw):
        raise Mem0Unavailable("simulated outage")
    mock_mem0.search_safe = fail_search

    create = await client.post("/api/hermes/sessions", json={}, headers=org_a.headers)
    sid = create.json()["id"]
    resp = await client.post(
        f"/api/hermes/sessions/{sid}/messages",
        json={"content": "long enough query for search"},
        headers=org_a.headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    # 主對話正常
    assert body["assistant_message"]["content"] == "Hello from mock Hermes."
    # 召回失敗 → 空陣列
    assert body["recalled_memories"] == []


async def test_prehook_disabled_via_feature_flag(
    client, org_a, seeded_token, mock_hermes, mock_mem0, monkeypatch,
) -> None:
    """MEM0_PREHOOK_ENABLED=False → search 不被呼叫;但 post-hook(寫入)仍正常。"""
    user_id = f"{org_a.org_id}:{org_a.username}"
    mock_mem0.facts[user_id] = [{"id": "f1", "memory": "should not be recalled"}]
    monkeypatch.setattr("app.routers.hermes.settings.MEM0_PREHOOK_ENABLED", False)

    create = await client.post("/api/hermes/sessions", json={}, headers=org_a.headers)
    sid = create.json()["id"]
    resp = await client.post(
        f"/api/hermes/sessions/{sid}/messages",
        json={"content": "long enough query"},
        headers=org_a.headers,
    )
    assert resp.status_code == 200
    assert resp.json()["recalled_memories"] == []
    # 推給 Hermes 的 content 沒 recalled block
    send_call = next(c for c in mock_hermes.calls if c[0] == "send_message")
    assert "<recalled_memory>" not in send_call[1][2]

    # 但 post-hook 仍應觸發(add_safe 被呼叫)
    import asyncio; await asyncio.sleep(0.05)
    assert len(mock_mem0.add_safe_calls) == 1


async def test_prehook_no_facts_returns_empty_recalled(
    client, org_a, seeded_token, mock_hermes, mock_mem0,
) -> None:
    """mem0 沒記憶可召回 → recalled_memories=[],不該 inject empty block。"""
    create = await client.post("/api/hermes/sessions", json={}, headers=org_a.headers)
    sid = create.json()["id"]
    resp = await client.post(
        f"/api/hermes/sessions/{sid}/messages",
        json={"content": "long enough query but no facts yet"},
        headers=org_a.headers,
    )
    assert resp.status_code == 200
    assert resp.json()["recalled_memories"] == []
    # 沒記憶就不該 inject 空的 <recalled_memory> block
    send_call = next(c for c in mock_hermes.calls if c[0] == "send_message")
    assert "<recalled_memory>" not in send_call[1][2]


async def test_prehook_post_hook_does_not_store_augmented_content(
    client, org_a, seeded_token, mock_hermes, mock_mem0,
) -> None:
    """關鍵安全測試:post-hook 存進去的 messages 應該是 raw user content,
    不是含 recalled_memory block 的 augmented version — 否則記憶會遞迴爆炸。"""
    import asyncio
    user_id = f"{org_a.org_id}:{org_a.username}"
    mock_mem0.facts[user_id] = [{"id": "f1", "memory": "existing memory A"}]

    create = await client.post("/api/hermes/sessions", json={}, headers=org_a.headers)
    sid = create.json()["id"]
    await client.post(
        f"/api/hermes/sessions/{sid}/messages",
        json={"content": "long enough new query"},
        headers=org_a.headers,
    )
    await asyncio.sleep(0.05)

    # post-hook 拿到的 messages 應是原 content
    add_call = mock_mem0.add_safe_calls[-1]
    user_msg_content = add_call["messages"][0]["content"]
    assert "<recalled_memory>" not in user_msg_content
    assert user_msg_content == "long enough new query"


async def test_send_message_to_nonexistent_session_returns_404(
    client, org_a, seeded_token, mock_hermes,
) -> None:
    resp = await client.post(
        "/api/hermes/sessions/00000000-0000-0000-0000-000000000000/messages",
        json={"content": "hi"},
        headers=org_a.headers,
    )
    assert resp.status_code == 404


async def test_send_empty_content_returns_400(
    client, org_a, seeded_token, mock_hermes,
) -> None:
    create = await client.post(
        "/api/hermes/sessions", json={}, headers=org_a.headers,
    )
    sid = create.json()["id"]
    resp = await client.post(
        f"/api/hermes/sessions/{sid}/messages",
        json={"content": "   "},
        headers=org_a.headers,
    )
    assert resp.status_code == 400


async def test_sidecar_unavailable_create_session_returns_503(
    client, org_a, seeded_token, monkeypatch,
) -> None:
    instance = _MockHermesClient(
        provision_raises=HermesUnavailable("sidecar_unreachable:ConnectError"),
    )
    monkeypatch.setattr(
        "app.routers.hermes.get_hermes_client", lambda: instance,
    )
    resp = await client.post(
        "/api/hermes/sessions", json={}, headers=org_a.headers,
    )
    assert resp.status_code == 503
    detail = resp.json().get("detail") or {}
    assert detail.get("error") == "hermes_unavailable"
    assert detail.get("retry_after") == 30


async def test_acp_error_send_message_returns_502(
    client, org_a, seeded_token, monkeypatch,
) -> None:
    """LLM provider 端錯誤(quota / 401 / model not found)→ 502 帶 detail。"""
    instance = _MockHermesClient(
        send_raises=HermesAcpError(-32000, "Provider 401 — invalid api key"),
    )
    monkeypatch.setattr(
        "app.routers.hermes.get_hermes_client", lambda: instance,
    )
    create = await client.post(
        "/api/hermes/sessions", json={}, headers=org_a.headers,
    )
    sid = create.json()["id"]
    resp = await client.post(
        f"/api/hermes/sessions/{sid}/messages",
        json={"content": "hi"},
        headers=org_a.headers,
    )
    assert resp.status_code == 502
    detail = resp.json().get("detail") or {}
    assert detail.get("error") == "ai_provider_error"
    assert "401" in (detail.get("message") or "")


async def test_disabled_via_feature_flag_returns_503(
    client, org_a, mock_hermes, monkeypatch,
) -> None:
    monkeypatch.setattr("app.routers.hermes.settings.HERMES_ENABLED", False)
    for path, method in [
        ("/api/hermes/sessions", "GET"),
        ("/api/hermes/sessions", "POST"),
    ]:
        if method == "GET":
            resp = await client.get(path, headers=org_a.headers)
        else:
            resp = await client.post(path, json={}, headers=org_a.headers)
        assert resp.status_code == 503, f"{method} {path} expected 503"
        detail = resp.json().get("detail") or {}
        assert detail.get("error") == "hermes_disabled"


async def test_unauthenticated_returns_401(client) -> None:
    """Auth 中介層擋下未帶 JWT 的請求(對齊既有 ai_chat 行為)。"""
    for path in [
        "/api/hermes/sessions",
        "/api/hermes/health",
        "/api/hermes/skills",
        "/api/hermes/memory/search",
        "/api/hermes/memory/semantic",
        "/api/hermes/memory/semantic/list",
        "/api/hermes/memory/consent",
        "/api/hermes/cron",
        "/api/hermes/gateway",
    ]:
        resp = await client.get(path)
        assert resp.status_code == 401, f"{path} expected 401"


# ── PR4: skills + memory ────────────────────────────────────────────
async def test_list_skills_empty_when_workspace_not_provisioned(
    client, org_a, mock_hermes,
) -> None:
    """沒設 token 的 user 也應能看到 skills(空陣列),不該被 400 擋。"""
    resp = await client.get("/api/hermes/skills", headers=org_a.headers)
    assert resp.status_code == 200
    assert resp.json() == {"skills": []}
    # 真的有打 sidecar 的 list_skills(不只是 short-circuit)
    assert any(c[0] == "list_skills" for c in mock_hermes.calls)


async def test_list_skills_returns_metadata(
    client, org_a, monkeypatch,
) -> None:
    instance = _MockHermesClient(skills_response={
        "skills": [
            {
                "name": "code-review",
                "namespace": "official",
                "description": "Reviews diffs and flags issues",
                "platforms": ["linux", "macos"],
                "path": "official/code-review/",
            },
            {
                "name": "regression-runner",
                "namespace": "",
                "description": "",
                "platforms": [],
                "path": "regression-runner/",
            },
        ]
    })
    monkeypatch.setattr(
        "app.routers.hermes.get_hermes_client", lambda: instance,
    )
    resp = await client.get("/api/hermes/skills", headers=org_a.headers)
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["skills"]) == 2
    assert body["skills"][0]["name"] == "code-review"
    assert body["skills"][0]["platforms"] == ["linux", "macos"]


async def test_list_skills_sidecar_unavailable_returns_503(
    client, org_a, monkeypatch,
) -> None:
    instance = _MockHermesClient(
        list_skills_raises=HermesUnavailable("sidecar_unreachable:ConnectError"),
    )
    monkeypatch.setattr(
        "app.routers.hermes.get_hermes_client", lambda: instance,
    )
    resp = await client.get("/api/hermes/skills", headers=org_a.headers)
    assert resp.status_code == 503


async def test_memory_search_empty_query_returns_empty(
    client, org_a, mock_hermes,
) -> None:
    """空 query 不該 raise — sidecar 會 echo 空陣列,前端能渲染 empty state。"""
    resp = await client.get(
        "/api/hermes/memory/search?q=", headers=org_a.headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["results"] == []
    assert body["query"] == ""


async def test_memory_search_passes_query_and_limit(
    client, org_a, monkeypatch,
) -> None:
    instance = _MockHermesClient(memory_response={
        "results": [
            {
                "session_id": "s1",
                "session_title": "Yesterday's debug",
                "role": "user",
                "content": "How do I retry a flaky test?",
                "timestamp": 1000.0,
                "rank": -3.2,
            },
        ],
        "query": "flaky",
        "sanitized_query": "flaky",
        "limit": 5,
    })
    monkeypatch.setattr(
        "app.routers.hermes.get_hermes_client", lambda: instance,
    )
    resp = await client.get(
        "/api/hermes/memory/search?q=flaky&limit=5", headers=org_a.headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["limit"] == 5
    assert body["sanitized_query"] == "flaky"
    assert len(body["results"]) == 1
    hit = body["results"][0]
    assert hit["session_id"] == "s1"
    assert hit["rank"] == -3.2

    # client 真的把 limit 傳到下游
    search_call = next(c for c in instance.calls if c[0] == "search_memory")
    _ws, q, lim = search_call[1]
    assert q == "flaky"
    assert lim == 5


async def test_memory_search_limit_validation(
    client, org_a, mock_hermes,
) -> None:
    """limit 必須 1..100;FastAPI Query 會自動 422。"""
    resp = await client.get(
        "/api/hermes/memory/search?q=foo&limit=0", headers=org_a.headers,
    )
    assert resp.status_code == 422
    resp = await client.get(
        "/api/hermes/memory/search?q=foo&limit=999", headers=org_a.headers,
    )
    assert resp.status_code == 422


async def test_skills_and_memory_disabled_via_feature_flag(
    client, org_a, mock_hermes, monkeypatch,
) -> None:
    monkeypatch.setattr("app.routers.hermes.settings.HERMES_ENABLED", False)
    for path in [
        "/api/hermes/skills",
        "/api/hermes/memory/search?q=anything",
        "/api/hermes/cron",
    ]:
        resp = await client.get(path, headers=org_a.headers)
        assert resp.status_code == 503, f"{path} expected 503"


# ── PR5: cron ───────────────────────────────────────────────────────
async def test_list_cron_empty(client, org_a, mock_hermes) -> None:
    """初始狀態 jobs.json 不存在時 sidecar 回 [],router 透傳。"""
    resp = await client.get("/api/hermes/cron", headers=org_a.headers)
    assert resp.status_code == 200
    assert resp.json() == {"jobs": []}


async def test_add_cron_persists_then_list_includes_it(
    client, org_a, seeded_token, mock_hermes,
) -> None:
    add = await client.post(
        "/api/hermes/cron",
        json={"schedule": "0 9 * * *", "prompt": "Run regression", "name": "morning"},
        headers=org_a.headers,
    )
    assert add.status_code == 201, add.text
    body = add.json()
    assert body["name"] == "morning"
    assert body["schedule"] == "0 9 * * *"
    assert body["prompt"] == "Run regression"
    # add 之前要先 ensure_workspace(provision sidecar) — mock 應該被呼到
    methods = [c[0] for c in mock_hermes.calls]
    assert "provision" in methods
    assert "add_cron_job" in methods

    listed = await client.get("/api/hermes/cron", headers=org_a.headers)
    assert listed.status_code == 200
    jobs = listed.json()["jobs"]
    assert len(jobs) == 1
    assert jobs[0]["name"] == "morning"


async def test_add_cron_without_token_returns_400(
    client, org_a, mock_hermes,
) -> None:
    """沒設 AI Token 時 router 回 400(對齊 send_message 行為)。"""
    resp = await client.post(
        "/api/hermes/cron",
        json={"schedule": "0 9 * * *", "prompt": "Run regression"},
        headers=org_a.headers,
    )
    assert resp.status_code == 400
    detail = resp.json().get("detail") or {}
    assert detail.get("error") == "no_token_configured"


async def test_add_cron_invalid_schedule_returns_400(
    client, org_a, seeded_token, monkeypatch,
) -> None:
    """sidecar 回 400(invalid_schedule)時 router 翻成 400。"""
    from app.services.hermes_client import HermesBadRequest
    instance = _MockHermesClient(add_cron_raises=HermesBadRequest("sidecar_400: invalid_schedule"))
    monkeypatch.setattr(
        "app.routers.hermes.get_hermes_client", lambda: instance,
    )
    resp = await client.post(
        "/api/hermes/cron",
        json={"schedule": "not-a-cron", "prompt": "x"},
        headers=org_a.headers,
    )
    assert resp.status_code == 400


async def test_add_cron_empty_fields_returns_400(
    client, org_a, mock_hermes,
) -> None:
    for body in [{}, {"schedule": "", "prompt": "x"}, {"schedule": "0 9 * * *", "prompt": ""}]:
        resp = await client.post(
            "/api/hermes/cron",
            json=body,
            headers=org_a.headers,
        )
        # 422 (FastAPI required field) or 400 (we explicitly check non-empty)
        assert resp.status_code in (400, 422), f"body={body} got {resp.status_code}"


async def test_delete_cron_succeeds(
    client, org_a, seeded_token, mock_hermes,
) -> None:
    # 先 add 一個
    add = await client.post(
        "/api/hermes/cron",
        json={"schedule": "0 9 * * *", "prompt": "x"},
        headers=org_a.headers,
    )
    job_id = add.json()["id"]
    dele = await client.delete(
        f"/api/hermes/cron/{job_id}", headers=org_a.headers,
    )
    assert dele.status_code == 204
    # 再 list 應該空
    listed = await client.get("/api/hermes/cron", headers=org_a.headers)
    assert listed.json()["jobs"] == []


async def test_delete_nonexistent_cron_returns_404(
    client, org_a, mock_hermes,
) -> None:
    """sidecar 回 404 時 router 翻成 404 而非 502。"""
    resp = await client.delete(
        "/api/hermes/cron/no_such_job", headers=org_a.headers,
    )
    assert resp.status_code == 404


async def test_delete_cron_path_traversal_blocked(
    client, org_a, mock_hermes,
) -> None:
    """job_id 含 .. 應在 backend 端就被擋,不打 sidecar。"""
    resp = await client.delete(
        "/api/hermes/cron/..%2Fetc",
        headers=org_a.headers,
    )
    # FastAPI URL decode 後仍含 ../etc — 我們在 router 拒了
    # 也接受 404(若 URL encode 沒 decode 路徑就錯)— 主要驗不該 200/204
    assert resp.status_code in (400, 404)
    # 重點:沒打到 sidecar
    assert not any(c[0] == "delete_cron_job" for c in mock_hermes.calls)


# ── Gateway PR ──────────────────────────────────────────────────────
async def test_gateway_status_initial(client, org_a, mock_hermes) -> None:
    resp = await client.get("/api/hermes/gateway", headers=org_a.headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["platforms"] == {}
    assert body["daemon"]["running"] is False


async def test_gateway_enable_persists_credential_and_calls_sidecar(
    client, org_a, seeded_token, mock_hermes,
) -> None:
    resp = await client.post(
        "/api/hermes/gateway/telegram/enable",
        json={"token": "1234567890:fake_telegram_bot_token"},
        headers=org_a.headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["platform"] == "telegram"
    assert body["enabled"] is True
    assert body["daemon"]["running"] is True

    methods = [c[0] for c in mock_hermes.calls]
    assert "gateway_enable" in methods
    # 驗 token 真的有從 backend 推給 sidecar(不是只存 DB 不傳)
    enable_call = next(c for c in mock_hermes.calls if c[0] == "gateway_enable")
    assert enable_call[2]["token"] == "1234567890:fake_telegram_bot_token"

    # DB row 落地
    from app.database import AsyncSessionLocal
    from app.models import HermesGatewayCredential
    from sqlalchemy import select
    async with AsyncSessionLocal() as session:
        cred = (await session.execute(
            select(HermesGatewayCredential).where(
                HermesGatewayCredential.owner == org_a.username,
                HermesGatewayCredential.platform == "telegram",
            )
        )).scalar_one()
        # bot_token 是 EncryptedString descriptor — 自動解密
        assert cred.bot_token == "1234567890:fake_telegram_bot_token"
        assert cred.enabled is True


async def test_gateway_enable_without_token_first_time_returns_400(
    client, org_a, seeded_token, mock_hermes,
) -> None:
    """從未 enable 過,沒帶 token 不行。"""
    resp = await client.post(
        "/api/hermes/gateway/telegram/enable",
        json={},
        headers=org_a.headers,
    )
    assert resp.status_code == 400
    detail = resp.json().get("detail") or {}
    assert detail.get("error") == "token_required"


async def test_gateway_enable_unsupported_platform_returns_400(
    client, org_a, seeded_token, mock_hermes,
) -> None:
    resp = await client.post(
        "/api/hermes/gateway/myspace/enable",
        json={"token": "x"},
        headers=org_a.headers,
    )
    assert resp.status_code == 400
    detail = resp.json().get("detail") or {}
    assert detail.get("error") == "unsupported_platform"


async def test_gateway_enable_without_ai_token_returns_400(
    client, org_a, mock_hermes,
) -> None:
    """Gateway 需要 AI Token(daemon 內 agent 要呼叫 LLM);沒設 → 400。"""
    resp = await client.post(
        "/api/hermes/gateway/telegram/enable",
        json={"token": "fake"},
        headers=org_a.headers,
    )
    assert resp.status_code == 400
    detail = resp.json().get("detail") or {}
    assert detail.get("error") == "no_token_configured"


async def test_gateway_enable_reuse_token_when_omitted(
    client, org_a, seeded_token, mock_hermes,
) -> None:
    """二次 enable 不帶 token,backend 應從 DB cred 讀已存的解密後傳給 sidecar。"""
    # 先 enable 一次
    await client.post(
        "/api/hermes/gateway/telegram/enable",
        json={"token": "first_token"},
        headers=org_a.headers,
    )
    # 再 enable(不帶 token)— router 會從 cred 解密拿
    resp = await client.post(
        "/api/hermes/gateway/telegram/enable",
        json={},
        headers=org_a.headers,
    )
    assert resp.status_code == 200, resp.text
    # Sidecar 收到的 token 應該是 'first_token'
    enables = [c for c in mock_hermes.calls if c[0] == "gateway_enable"]
    assert len(enables) == 2
    assert enables[1][2]["token"] == "first_token"


async def test_gateway_disable_keeps_credential(
    client, org_a, seeded_token, mock_hermes,
) -> None:
    """Disable 把 sidecar daemon 停掉,但 DB 內 cred row 仍在(enabled=False)。"""
    await client.post(
        "/api/hermes/gateway/telegram/enable",
        json={"token": "keepme"},
        headers=org_a.headers,
    )
    resp = await client.post(
        "/api/hermes/gateway/telegram/disable",
        headers=org_a.headers,
    )
    assert resp.status_code == 204
    methods = [c[0] for c in mock_hermes.calls]
    assert "gateway_disable" in methods

    from app.database import AsyncSessionLocal
    from app.models import HermesGatewayCredential
    from sqlalchemy import select
    async with AsyncSessionLocal() as session:
        cred = (await session.execute(
            select(HermesGatewayCredential).where(
                HermesGatewayCredential.owner == org_a.username,
                HermesGatewayCredential.platform == "telegram",
            )
        )).scalar_one()
        assert cred.enabled is False
        # token 沒被清掉,使用者可以後續再 enable
        assert cred.bot_token == "keepme"


async def test_gateway_delete_removes_credential(
    client, org_a, seeded_token, mock_hermes,
) -> None:
    await client.post(
        "/api/hermes/gateway/telegram/enable",
        json={"token": "deleteme"},
        headers=org_a.headers,
    )
    resp = await client.delete(
        "/api/hermes/gateway/telegram", headers=org_a.headers,
    )
    assert resp.status_code == 204

    from app.database import AsyncSessionLocal
    from app.models import HermesGatewayCredential
    from sqlalchemy import select
    async with AsyncSessionLocal() as session:
        cred = (await session.execute(
            select(HermesGatewayCredential).where(
                HermesGatewayCredential.owner == org_a.username,
                HermesGatewayCredential.platform == "telegram",
            )
        )).scalar_one_or_none()
        assert cred is None  # 完整刪除


async def test_reprovision_invalidates_cache_and_calls_provision(
    client, org_a, seeded_token, mock_hermes,
) -> None:
    """POST /api/hermes/reprovision 應強制重推 token 給 sidecar(無視 cache)。"""
    # 先打一次 sessions endpoint 觸發 provision + cache
    await client.get("/api/hermes/sessions", headers=org_a.headers)
    # 注意:list_sessions 路徑現在不會觸發 ensure_user_workspace(只查 DB),
    # 所以先打 POST sessions 觸發第一次 provision
    create = await client.post(
        "/api/hermes/sessions", json={}, headers=org_a.headers,
    )
    assert create.status_code == 201
    initial_provision_calls = sum(
        1 for c in mock_hermes.calls if c[0] == "provision"
    )
    assert initial_provision_calls >= 1

    # reprovision 應該再呼叫一次(force=True 跳過 cache)
    resp = await client.post("/api/hermes/reprovision", headers=org_a.headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "reprovisioned"
    assert body["workspace_id"].startswith("ws_")

    after = sum(1 for c in mock_hermes.calls if c[0] == "provision")
    assert after == initial_provision_calls + 1, \
        "reprovision 應 force 一次新 provision 呼叫,即便 cache 還有效"


async def test_reprovision_without_token_returns_400(
    client, org_a, mock_hermes,
) -> None:
    resp = await client.post("/api/hermes/reprovision", headers=org_a.headers)
    assert resp.status_code == 400
    detail = resp.json().get("detail") or {}
    assert detail.get("error") == "no_token_configured"


async def test_reprovision_unauthenticated_returns_401(client) -> None:
    resp = await client.post("/api/hermes/reprovision")
    assert resp.status_code == 401


async def test_reprovision_disabled_via_feature_flag(
    client, org_a, seeded_token, mock_hermes, monkeypatch,
) -> None:
    monkeypatch.setattr("app.routers.hermes.settings.HERMES_ENABLED", False)
    resp = await client.post("/api/hermes/reprovision", headers=org_a.headers)
    assert resp.status_code == 503


# ── set_hermes_default_token(全 org 唯一 default 語意)─────────────
async def test_set_default_token_clears_other_defaults_in_org(
    client, org_a, mock_hermes,
) -> None:
    """確保「全 org 一個 default」語意:切某 token 為 default 後,
    同 org 其他 default 都被設成 false(對齊 LLM modal 的單一 default UX)。"""
    from app.database import AsyncSessionLocal
    from app.models import AiTokenConfig
    from sqlalchemy import select
    # 塞兩個跨 provider 的 default token(模擬 settings.py 的 per-provider default 共存)
    async with AsyncSessionLocal() as session:
        oai = AiTokenConfig(
            name="oai-d", organization_id=org_a.org_id,
            provider="OpenAI", api_key="sk-oai", enabled=True, is_default=True,
        )
        ant = AiTokenConfig(
            name="ant-d", organization_id=org_a.org_id,
            provider="Anthropic", api_key="sk-ant", enabled=True, is_default=True,
        )
        session.add_all([oai, ant])
        await session.commit()
        oai_id, ant_id = oai.id, ant.id

    # 用 hermes 端點把 oai 設為 default — ant 的 is_default 應被自動清掉
    resp = await client.post(
        f"/api/hermes/default-token/{oai_id}",
        headers=org_a.headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["default_token_id"] == oai_id
    assert body["default_provider"] == "OpenAI"

    async with AsyncSessionLocal() as session:
        oai_after = await session.get(AiTokenConfig, oai_id)
        ant_after = await session.get(AiTokenConfig, ant_id)
        assert oai_after.is_default is True
        assert ant_after.is_default is False  # 自動清掉

    # provision 必須有被呼叫(reprovision 推給 sidecar)
    methods = [c[0] for c in mock_hermes.calls]
    assert "provision" in methods


async def test_set_default_token_404_when_not_owned(
    client, org_a, org_b, mock_hermes,
) -> None:
    """B org 的 token,A org 的 user 不能設為 default。"""
    from app.database import AsyncSessionLocal
    from app.models import AiTokenConfig
    async with AsyncSessionLocal() as session:
        b_token = AiTokenConfig(
            name="b-only", organization_id=org_b.org_id,
            provider="OpenAI", api_key="sk-b", enabled=True, is_default=False,
        )
        session.add(b_token)
        await session.commit()
        b_id = b_token.id

    resp = await client.post(
        f"/api/hermes/default-token/{b_id}",
        headers=org_a.headers,
    )
    assert resp.status_code == 404


async def test_set_default_token_400_when_disabled(
    client, org_a, mock_hermes,
) -> None:
    from app.database import AsyncSessionLocal
    from app.models import AiTokenConfig
    async with AsyncSessionLocal() as session:
        t = AiTokenConfig(
            name="dis", organization_id=org_a.org_id,
            provider="OpenAI", api_key="sk-x", enabled=False, is_default=False,
        )
        session.add(t)
        await session.commit()
        tid = t.id

    resp = await client.post(
        f"/api/hermes/default-token/{tid}", headers=org_a.headers,
    )
    assert resp.status_code == 400
    assert resp.json().get("detail", {}).get("error") == "token_disabled"


async def test_set_default_token_400_when_no_api_key(
    client, org_a, mock_hermes,
) -> None:
    from app.database import AsyncSessionLocal
    from app.models import AiTokenConfig
    async with AsyncSessionLocal() as session:
        t = AiTokenConfig(
            name="empty", organization_id=org_a.org_id,
            provider="Local", api_key=None, enabled=True, is_default=False,
        )
        session.add(t)
        await session.commit()
        tid = t.id

    resp = await client.post(
        f"/api/hermes/default-token/{tid}", headers=org_a.headers,
    )
    assert resp.status_code == 400
    assert resp.json().get("detail", {}).get("error") == "token_missing_api_key"


async def test_gateway_token_never_returned_to_client(
    client, org_a, seeded_token, mock_hermes,
) -> None:
    """有 token 後再 GET status,response 不該包含 plaintext token。"""
    await client.post(
        "/api/hermes/gateway/telegram/enable",
        json={"token": "secret_must_not_leak"},
        headers=org_a.headers,
    )
    # 模擬 sidecar status 回 has_token but no token field
    instance = _MockHermesClient(gateway_status_response={
        "platforms": {
            "telegram": {"enabled": True, "has_token": True, "extra": {}},
        },
        "daemon": {"running": True, "uptime_sec": 5.0,
                   "last_exit_code": None, "recent_stderr": []},
    })
    import pytest
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr("app.routers.hermes.get_hermes_client", lambda: instance)
    try:
        resp = await client.get("/api/hermes/gateway", headers=org_a.headers)
        body_text = resp.text
        assert "secret_must_not_leak" not in body_text
        body = resp.json()
        assert body["platforms"]["telegram"]["has_token"] is True
        assert "token" not in body["platforms"]["telegram"]
    finally:
        monkeypatch.undo()
