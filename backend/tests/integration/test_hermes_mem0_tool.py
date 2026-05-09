"""PR3:Backend ↔ mem0 sidecar 的 MCP tool wiring 驗證。

PR3 把 mem0 變成 Hermes ACP 子進程能主動 invoke 的 MCP tool — 牽動 4 條路徑:
1. routers/hermes.py:create_session 把 mcp_servers 帶給 hermes(只在有 token + 有
   embedder + feature flag 開啟時)
2. services/hermes_provisioning.py:ensure_user_workspace 同步推 llm_config 給 mem0
3. routers/settings.py:create/update/delete_ai_token 三處走 sync_mem0_llm_config
   (push / clear)
4. mem0 sidecar 這邊吃 cache(已在 PR1 mem0/tests/test_mcp_endpoint.py 驗了)

這個檔的測試只覆 backend 端,mock 掉 hermes/mem0 sidecar 的真 client。
"""
from __future__ import annotations

import pytest

from app.database import AsyncSessionLocal
from app.models import AiTokenConfig
from app.services.hermes_client import HermesClient
from app.services.hermes_provisioning import invalidate_user_workspace

pytestmark = pytest.mark.integration


# ── Mocks ─────────────────────────────────────────────────────────────
class _RecordingHermesClient(HermesClient):
    """記錄所有呼叫,回固定假回應,不打網路。"""

    def __init__(self, *, next_session_id: str = "sess-pr3-mem0-test"):
        self.calls: list[tuple[str, tuple, dict]] = []
        self._next_session_id = next_session_id

    async def healthcheck(self) -> bool: return True
    async def aclose(self) -> None: pass

    async def provision(self, workspace_id, provider, api_key,
                        base_url=None, system_prompt=None) -> None:
        self.calls.append(("provision", (workspace_id,), {
            "provider": provider, "base_url": base_url,
        }))

    async def create_session(self, workspace_id, mcp_servers=None) -> dict:
        self.calls.append(("create_session", (workspace_id,), {
            "mcp_servers": mcp_servers,
        }))
        return {"session_id": self._next_session_id, "models": None}

    async def list_sessions(self, workspace_id) -> dict:
        return {"sessions": []}

    async def send_message(self, workspace_id, session_id, content) -> dict:
        return {"session_id": session_id, "content": "stub", "stop_reason": "end_turn"}

    async def list_skills(self, workspace_id) -> dict: return {"skills": []}
    async def search_memory(self, workspace_id, query, limit=20) -> dict:
        return {"results": [], "query": query, "limit": limit}
    async def list_cron_jobs(self, workspace_id) -> dict: return {"jobs": []}
    async def add_cron_job(self, workspace_id, *, schedule, prompt, name=None):
        return {}
    async def delete_cron_job(self, workspace_id, job_id) -> None: pass
    async def gateway_status(self, workspace_id) -> dict:
        return {"platforms": {}, "daemon": {"running": False}}
    async def gateway_enable(self, workspace_id, platform, *, token, extra=None):
        return {}
    async def gateway_disable(self, workspace_id, platform) -> None: pass


class _RecordingMem0Client:
    """只記錄 push/clear,別的 method 都回 no-op 假值。"""

    def __init__(self, *, push_succeeds: bool = True):
        self.push_calls: list[dict] = []
        self.clear_calls: list[str] = []
        self._push_succeeds = push_succeeds

    async def push_llm_config_safe(self, user_id, llm_config, embedder_config) -> bool:
        self.push_calls.append({
            "user_id": user_id,
            "llm_config": llm_config,
            "embedder_config": embedder_config,
        })
        return self._push_succeeds

    async def clear_llm_config_safe(self, user_id) -> bool:
        self.clear_calls.append(user_id)
        return True

    # 既有 router pre/post hook 也會呼叫這些 — 給空 stub 不擋
    async def healthcheck(self) -> bool: return True
    async def add_safe(self, **kw) -> bool: return True
    async def search_safe(self, **kw) -> list: return []
    async def aclose(self) -> None: pass


@pytest.fixture
def mock_hermes(monkeypatch):
    instance = _RecordingHermesClient()
    monkeypatch.setattr("app.routers.hermes.get_hermes_client", lambda: instance)
    yield instance


@pytest.fixture
def mock_mem0(monkeypatch):
    """同時 patch 兩個 import 點 — router(send_message 用)+ service(ensure +
    sync_mem0_llm_config 用)。"""
    instance = _RecordingMem0Client()
    monkeypatch.setattr("app.routers.hermes.get_mem0_client", lambda: instance)
    monkeypatch.setattr("app.services.mem0_client.get_mem0_client", lambda: instance)
    yield instance


# ── Token fixtures ────────────────────────────────────────────────────
async def _seed_token(org, *, provider="OpenAI", api_key="sk-test-fake",
                      model="gpt-4o-mini") -> AiTokenConfig:
    async with AsyncSessionLocal() as s:
        t = AiTokenConfig(
            name=f"pr3-{provider.lower()}-{org.username}",
            organization_id=org.org_id,
            provider=provider,
            api_key=api_key,
            model=model,
            enabled=True,
            is_default=True,
        )
        s.add(t)
        await s.commit()
        await s.refresh(t)
    invalidate_user_workspace(org.username)
    return t


@pytest.fixture
async def openai_token(org_a):
    return await _seed_token(org_a, provider="OpenAI")


@pytest.fixture
async def anthropic_token(org_a):
    """Anthropic-only — primary token 沒 embedder,需要 fallback 才能用 mem0。"""
    return await _seed_token(
        org_a, provider="Anthropic",
        api_key="sk-ant-test-fake", model="claude-3-5-haiku-latest",
    )


@pytest.fixture
async def gemini_token(org_a):
    return await _seed_token(
        org_a, provider="Gemini",
        api_key="gem-test-fake", model="gemini-2.0-flash",
    )


async def _seed_secondary_token(org, *, provider, api_key, model):
    """Seed 第二把 token(non-default)— 給 fallback 測試用。"""
    async with AsyncSessionLocal() as s:
        t = AiTokenConfig(
            name=f"secondary-{provider.lower()}-{org.username}",
            organization_id=org.org_id,
            provider=provider, api_key=api_key, model=model,
            enabled=True, is_default=False,
        )
        s.add(t)
        await s.commit()
        await s.refresh(t)
    return t


# ── Tests ─────────────────────────────────────────────────────────────
async def test_create_session_passes_mcp_servers_to_hermes(
    client, org_a, openai_token, mock_hermes, mock_mem0,
):
    """有 token + 有 embedder → hermes.create_session 帶 mcp_servers。

    headers 必須含 X-Sidecar-Auth + X-Mem0-User-Id;url 取自 settings。
    """
    resp = await client.post(
        "/api/hermes/sessions", json={"title": "pr3-mcp"},
        headers=org_a.headers,
    )
    assert resp.status_code == 201, resp.text

    create_call = next(c for c in mock_hermes.calls if c[0] == "create_session")
    mcp_servers = create_call[2]["mcp_servers"]
    assert isinstance(mcp_servers, list) and len(mcp_servers) == 1, mcp_servers
    entry = mcp_servers[0]
    assert entry["name"] == "memory"
    assert entry["url"].endswith("/mcp/mcp")
    header_names = {h["name"] for h in entry["headers"]}
    assert "X-Sidecar-Auth" in header_names
    assert "X-Mem0-User-Id" in header_names
    user_id_header = next(h for h in entry["headers"] if h["name"] == "X-Mem0-User-Id")
    # mem0_user_id 規則:`{org_id or 'default'}:{username}`
    assert user_id_header["value"] == f"{org_a.org_id}:{org_a.username}"


async def test_create_session_pushes_llm_config_to_mem0(
    client, org_a, openai_token, mock_hermes, mock_mem0,
):
    """ensure_user_workspace 階段同步 push llm_config 給 mem0 sidecar。"""
    resp = await client.post(
        "/api/hermes/sessions", json={}, headers=org_a.headers,
    )
    assert resp.status_code == 201, resp.text

    assert len(mock_mem0.push_calls) == 1, mock_mem0.push_calls
    call = mock_mem0.push_calls[0]
    assert call["user_id"] == f"{org_a.org_id}:{org_a.username}"
    assert call["llm_config"]["provider"] == "openai"
    assert call["embedder_config"]["provider"] == "openai"


async def test_create_session_omits_mcp_when_tool_disabled(
    client, org_a, openai_token, mock_hermes, mock_mem0, monkeypatch,
):
    """MEM0_HERMES_TOOL_ENABLED=False → mcp_servers=[](feature flag kill switch)。"""
    monkeypatch.setattr("app.config.settings.MEM0_HERMES_TOOL_ENABLED", False)

    resp = await client.post(
        "/api/hermes/sessions", json={}, headers=org_a.headers,
    )
    assert resp.status_code == 201

    create_call = next(c for c in mock_hermes.calls if c[0] == "create_session")
    assert create_call[2]["mcp_servers"] == []
    # mem0 push 也跟著 skip
    assert mock_mem0.push_calls == []


async def test_create_session_omits_mcp_for_anthropic_only_no_fallback(
    client, org_a, anthropic_token, mock_hermes, mock_mem0,
):
    """Anthropic-only(整 org 沒其他 token)→ mcp_servers=[];mem0 push 也跳過。

    Anthropic 沒 embedder API,且找不到 OpenAI/Gemini fallback → mem0 跑不起來,
    fail-fast 不帶 mcp_servers,避免 LLM 看到 tool 卻每次 call 都拿 friendly error。
    """
    resp = await client.post(
        "/api/hermes/sessions", json={}, headers=org_a.headers,
    )
    assert resp.status_code == 201

    create_call = next(c for c in mock_hermes.calls if c[0] == "create_session")
    assert create_call[2]["mcp_servers"] == []
    assert mock_mem0.push_calls == []


async def test_anthropic_primary_uses_openai_fallback_for_embedder(
    client, org_a, anthropic_token, mock_hermes, mock_mem0,
):
    """Anthropic primary + 同 org 內有 OpenAI(non-default)→ 用 OpenAI 做 embedding。

    push 的 llm_config 用 Anthropic key(對話 + fact extraction),
    embedder_config 用 OpenAI key(向量檢索)。MCP tool 也帶上(因為現在能跑了)。
    """
    await _seed_secondary_token(
        org_a, provider="OpenAI",
        api_key="sk-fallback-openai", model="gpt-4o-mini",
    )
    invalidate_user_workspace(org_a.username)  # 清 cache 確保 ensure 重跑

    resp = await client.post(
        "/api/hermes/sessions", json={}, headers=org_a.headers,
    )
    assert resp.status_code == 201, resp.text

    # mcp_servers 帶上(因為 fallback 救回來了)
    create_call = next(c for c in mock_hermes.calls if c[0] == "create_session")
    mcp = create_call[2]["mcp_servers"]
    assert isinstance(mcp, list) and len(mcp) == 1, mcp

    # push 的 config:LLM=anthropic,embedder=openai(fallback 正確生效)
    assert len(mock_mem0.push_calls) == 1
    call = mock_mem0.push_calls[0]
    assert call["llm_config"]["provider"] == "anthropic"
    assert call["embedder_config"]["provider"] == "openai"
    # embedder 用的是 fallback token 的 key,不是 Anthropic 的
    assert call["embedder_config"]["config"]["api_key"] == "sk-fallback-openai"


async def test_anthropic_primary_uses_gemini_fallback_when_no_openai(
    client, org_a, anthropic_token, mock_hermes, mock_mem0,
):
    """Anthropic primary + 同 org 只有 Gemini(沒 OpenAI)→ 走 Gemini embedder。

    Verify provider 排序:OpenAI 先,沒 OpenAI 才退到 Gemini(本檔關鍵的 priority 驗證)。
    """
    await _seed_secondary_token(
        org_a, provider="Gemini",
        api_key="gem-fallback", model="gemini-2.0-flash",
    )
    invalidate_user_workspace(org_a.username)

    resp = await client.post(
        "/api/hermes/sessions", json={}, headers=org_a.headers,
    )
    assert resp.status_code == 201

    assert len(mock_mem0.push_calls) == 1
    call = mock_mem0.push_calls[0]
    assert call["llm_config"]["provider"] == "anthropic"
    assert call["embedder_config"]["provider"] == "gemini"


async def test_create_session_works_with_gemini_only(
    client, org_a, gemini_token, mock_hermes, mock_mem0,
):
    """Gemini-only 自帶 embedder → 直接用 Gemini 做 LLM + embedding。"""
    resp = await client.post(
        "/api/hermes/sessions", json={}, headers=org_a.headers,
    )
    assert resp.status_code == 201

    assert len(mock_mem0.push_calls) == 1
    call = mock_mem0.push_calls[0]
    assert call["llm_config"]["provider"] == "gemini"
    assert call["embedder_config"]["provider"] == "gemini"


async def test_create_session_succeeds_when_mem0_push_fails(
    client, org_a, openai_token, mock_hermes, monkeypatch,
):
    """mem0 sidecar 掛點 → push 失敗,但 create_session 仍成功(graceful)。

    使用者下次在對話中 invoke MCP tool 時會拿到 friendly error,但 session 建得起來、
    主對話流程不被擋(plan §6)。
    """
    fail_mem0 = _RecordingMem0Client(push_succeeds=False)
    monkeypatch.setattr("app.routers.hermes.get_mem0_client", lambda: fail_mem0)
    monkeypatch.setattr("app.services.mem0_client.get_mem0_client", lambda: fail_mem0)

    resp = await client.post(
        "/api/hermes/sessions", json={}, headers=org_a.headers,
    )
    assert resp.status_code == 201, resp.text
    # push 還是被嘗試了一次,但回 False
    assert len(fail_mem0.push_calls) == 1


# ── Settings router hooks ─────────────────────────────────────────────
async def test_create_ai_token_pushes_to_mem0(
    client, org_a, mock_mem0,
):
    """POST /api/settings/ai-tokens(新建 default token) → mem0 push 被呼叫。

    走 sync_mem0_llm_config 路徑;新建 enabled+default 的 OpenAI token 會挑成
    default 並推給 mem0。
    """
    resp = await client.post(
        "/api/settings/ai-tokens",
        json={
            "name": "pr3-create",
            "provider": "OpenAI",
            "api_key": "sk-test-create",
            "model": "gpt-4o-mini",
            "enabled": True,
            "is_default": True,
        },
        headers=org_a.headers,
    )
    assert resp.status_code == 201, resp.text
    assert len(mock_mem0.push_calls) == 1
    assert mock_mem0.push_calls[0]["user_id"] == f"{org_a.org_id}:{org_a.username}"


async def test_delete_ai_token_clears_mem0(
    client, org_a, openai_token, mock_mem0,
):
    """DELETE /api/settings/ai-tokens/{id}(刪掉唯一 default) → mem0 clear。

    刪掉後 pick_token_for_user 回 None(沒其他 token),sync_mem0_llm_config 走
    clear 路徑。
    """
    resp = await client.delete(
        f"/api/settings/ai-tokens/{openai_token.id}",
        headers=org_a.headers,
    )
    assert resp.status_code == 204
    assert org_a.username in [
        u for u in [c for c in mock_mem0.clear_calls]
    ] or len(mock_mem0.clear_calls) == 1
    # mem0 user_id 不是 username,是 `{org_id}:{username}`
    assert mock_mem0.clear_calls == [f"{org_a.org_id}:{org_a.username}"]


async def test_update_ai_token_pushes_new_config(
    client, org_a, openai_token, mock_mem0,
):
    """PUT /api/settings/ai-tokens/{id}(改 model)→ 新 default config push 給 mem0。"""
    resp = await client.put(
        f"/api/settings/ai-tokens/{openai_token.id}",
        json={"model": "gpt-4o"},
        headers=org_a.headers,
    )
    assert resp.status_code == 200, resp.text
    assert len(mock_mem0.push_calls) == 1
    assert mock_mem0.push_calls[0]["llm_config"]["config"]["model"] == "gpt-4o"
