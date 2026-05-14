"""OpenClaw sidecar HTTP client (Phase 3 scaffold).

對應 hermes_client.py 的角色:把 backend 與 openclaw sidecar 的通訊封一層。
目前只 wire 三個方法:
  - healthz()       — liveness probe
  - status()        — 看 daemon ready 沒(缺 OAuth credential / data root)
  - provision()     — 把 ChatGPT OAuth token 推進去(workspace + .env)

`chat()` 暫不實作 — sidecar 那邊 /v1/chat 回 501。Phase 3.5 wire 完整 chat
路徑(routing 在 ai_chat router 依 user.preferred_agent 切到這支 client)。
"""
from __future__ import annotations

import json
import logging
import os
from typing import Optional

import httpx

LOG = logging.getLogger(__name__)


class OpenClawError(RuntimeError):
    """OpenClaw sidecar 回非 2xx 時 raise。"""


class OpenClawClient:
    """非常薄的 HTTP client(對齊 hermes_client.py 的 httpx pattern)。
    Backend 路徑用量低(只在切到 openclaw runtime 時才打)— 沒做 connection
    pooling 的必要。"""

    def __init__(self, base_url: Optional[str] = None, auth_token: Optional[str] = None,
                 timeout_sec: float = 30.0):
        self.base_url = (base_url or os.environ.get("OPENCLAW_BASE_URL")
                         or "http://openclaw:7950").rstrip("/")
        self.auth_token = auth_token or os.environ.get("OPENCLAW_SIDECAR_AUTH_TOKEN", "")
        self.timeout = timeout_sec

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.auth_token:
            h["X-Sidecar-Auth"] = self.auth_token
        return h

    async def healthz(self) -> dict:
        """GET /healthz — 不帶 auth(同 hermes/mem0 pattern)。"""
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.get(f"{self.base_url}/healthz")
            if r.status_code != 200:
                raise OpenClawError(f"healthz: HTTP {r.status_code}")
            return r.json()

    async def status(self) -> dict:
        """GET /v1/status — 回 {ready, phase, reasons}。"""
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.get(f"{self.base_url}/v1/status", headers=self._headers())
            if r.status_code != 200:
                raise OpenClawError(f"status: HTTP {r.status_code}")
            return r.json()

    async def provision(self, workspace_id: str, oauth_token: str) -> dict:
        """POST /v1/provision — 把 ChatGPT OAuth token 推給 sidecar。

        backend 端的 OAuth callback 拿到 token 後呼叫此方法;sidecar 寫進
        /opt/openclaw-data/<workspace_id>/.env。
        """
        if not workspace_id or not oauth_token:
            raise ValueError("workspace_id and oauth_token required")
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.post(
                f"{self.base_url}/v1/provision",
                headers=self._headers(),
                json={"workspace_id": workspace_id, "oauth_token": oauth_token},
            )
            if r.status_code >= 400:
                raise OpenClawError(f"provision: HTTP {r.status_code} — {r.text[:200]}")
            return r.json()

    async def chat(self, workspace_id: str, prompt: str) -> dict:
        """POST /v1/chat — spawn openclaw CLI 於 sidecar。

        成功回 `{"content": "<stdout>", "stderr": ...}`。
        Sidecar 端任何錯誤(openclaw 沒裝 / workspace 沒 provision / cli 失敗 /
        timeout)都會以非 2xx HTTP 回來;這裡統一 raise OpenClawError 讓 caller
        graceful fallback 回 Hermes。
        """
        # chat 路徑可能等 60s+(openclaw subprocess 跑 LLM),用較長 timeout
        async with httpx.AsyncClient(timeout=90.0) as client:
            r = await client.post(
                f"{self.base_url}/v1/chat",
                headers=self._headers(),
                json={"workspace_id": workspace_id, "prompt": prompt},
            )
            if r.status_code >= 400:
                # 把 sidecar 結構化錯誤往上吐(json parse 失敗就 raw text)
                try:
                    err = r.json()
                    code = err.get("error", f"http_{r.status_code}")
                    msg = err.get("message") or err.get("stderr") or r.text
                except Exception:
                    code = f"http_{r.status_code}"
                    msg = r.text[:300]
                raise OpenClawError(f"{code}: {msg}")
            return r.json()


# Singleton(對齊 hermes_client 的 _global_hermes_client pattern)
_GLOBAL_CLIENT: Optional[OpenClawClient] = None


def get_openclaw_client() -> OpenClawClient:
    global _GLOBAL_CLIENT
    if _GLOBAL_CLIENT is None:
        _GLOBAL_CLIENT = OpenClawClient()
    return _GLOBAL_CLIENT


# ── User-level provisioning helper ────────────────────────────────────
# 對齊 hermes_provisioning.ensure_user_workspace 的 cache 模式。OpenClaw 路徑
# 用 user 的 openai-oauth token push 給 sidecar。

import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession  # noqa: F401
    from app.models.user import User  # noqa: F401

_OC_PROVISIONED_CACHE: dict[str, float] = {}
_OC_CACHE_TTL_SEC = 300


def invalidate_openclaw_workspace(username: str) -> None:
    _OC_PROVISIONED_CACHE.pop(username, None)


async def ensure_openclaw_provisioned(
    user, db, *, force: bool = False,
) -> tuple[str, str]:
    """確保 user 的 OpenClaw workspace 已 provision。

    回 (workspace_id, oauth_token_first8_for_log)。失敗 raise OpenClawError —
    caller (hermes router send_message) 接到後 graceful fallback 回 Hermes。

    workspace_id 沿用 hermes_provisioning.workspace_id_for_user 同個 sha256 hash —
    跨 sidecar 一致(方便 debug 對照 + 未來合管理 UI)。
    """
    from app.services.hermes_provisioning import workspace_id_for_user
    from app.models.ai_token_config import AiTokenConfig
    from sqlalchemy import select

    ws = workspace_id_for_user(user)
    now = time.monotonic()
    cached_at = _OC_PROVISIONED_CACHE.get(user.username)
    if not force and cached_at and now - cached_at < _OC_CACHE_TTL_SEC:
        return ws, "cached"

    # 撈該 user (or org) 可餵給 openclaw CLI 的 token。
    # OpenClaw sidecar(supervisor.py)實際只把 token 寫成 `OPENAI_API_KEY` env var
    # 餵給 `openclaw agent --local`,CLI 走 OpenAI-compatible 路徑 —— 所以一般
    # `provider="OpenAI"` 的 sk-... API key 就能用,不必走 ChatGPT OAuth flow。
    # 也仍接受 `openai-oauth`(若日後真的接 OAuth callback 時用)。
    # 優先序:is_default → provider=OpenAI → 任一可用。
    stmt = select(AiTokenConfig).where(
        AiTokenConfig.enabled.is_(True),
    )
    if user.organization_id:
        stmt = stmt.where(AiTokenConfig.organization_id == user.organization_id)
    rows = (await db.execute(stmt)).scalars().all()
    candidates = [
        t for t in rows
        if (t.provider or "").lower() in {"openai", "openai-oauth"}
        and t.api_key
    ]
    if not candidates:
        raise OpenClawError("no_openai_token_configured")
    candidates.sort(key=lambda t: (
        0 if t.is_default else 1,
        0 if (t.provider or "").lower() == "openai-oauth" else 1,
        t.name or "",
    ))
    cfg = candidates[0]

    client = get_openclaw_client()
    try:
        await client.provision(workspace_id=ws, oauth_token=cfg.api_key or "")
    except OpenClawError:
        # 不 cache 失敗的 provision — 下次重試
        raise

    _OC_PROVISIONED_CACHE[user.username] = now
    LOG.info("provisioned openclaw workspace user=%s ws=%s", user.username, ws)
    return ws, (cfg.api_key or "")[:8] + "***"
