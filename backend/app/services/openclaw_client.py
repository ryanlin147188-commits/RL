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

import logging
import os
from typing import Optional

import aiohttp

LOG = logging.getLogger(__name__)


class OpenClawError(RuntimeError):
    """OpenClaw sidecar 回非 2xx 時 raise。"""


class OpenClawClient:
    """非常薄的 HTTP client。每個方法獨立建 session(避免 lifecycle 與 backend
    主 aiohttp session 糾纏)。Backend 路徑用量低(只在切到 openclaw runtime 時
    才打)— 沒做 connection pooling 的必要。"""

    def __init__(self, base_url: Optional[str] = None, auth_token: Optional[str] = None,
                 timeout_sec: float = 30.0):
        self.base_url = (base_url or os.environ.get("OPENCLAW_BASE_URL")
                         or "http://openclaw:7950").rstrip("/")
        self.auth_token = auth_token or os.environ.get("OPENCLAW_SIDECAR_AUTH_TOKEN", "")
        self.timeout = aiohttp.ClientTimeout(total=timeout_sec)

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.auth_token:
            h["X-Sidecar-Auth"] = self.auth_token
        return h

    async def healthz(self) -> dict:
        """GET /healthz — 不帶 auth(同 hermes/mem0 pattern)。"""
        url = f"{self.base_url}/healthz"
        async with aiohttp.ClientSession(timeout=self.timeout) as session:
            async with session.get(url) as r:
                if r.status != 200:
                    raise OpenClawError(f"healthz: HTTP {r.status}")
                return await r.json()

    async def status(self) -> dict:
        """GET /v1/status — 回 {ready, phase, reasons}。"""
        url = f"{self.base_url}/v1/status"
        async with aiohttp.ClientSession(timeout=self.timeout) as session:
            async with session.get(url, headers=self._headers()) as r:
                if r.status != 200:
                    raise OpenClawError(f"status: HTTP {r.status}")
                return await r.json()

    async def provision(self, workspace_id: str, oauth_token: str) -> dict:
        """POST /v1/provision — 把 ChatGPT OAuth token 推給 sidecar。

        backend 端的 OAuth callback 拿到 token 後呼叫此方法;sidecar 寫進
        /opt/openclaw-data/<workspace_id>/.env。Phase 3.5 加 daemon spawn 後,
        這步會同時觸發 daemon 重啟讀新 env。
        """
        if not workspace_id or not oauth_token:
            raise ValueError("workspace_id and oauth_token required")
        url = f"{self.base_url}/v1/provision"
        async with aiohttp.ClientSession(timeout=self.timeout) as session:
            async with session.post(
                url,
                headers=self._headers(),
                json={"workspace_id": workspace_id, "oauth_token": oauth_token},
            ) as r:
                if r.status >= 400:
                    body = await r.text()
                    raise OpenClawError(f"provision: HTTP {r.status} — {body[:200]}")
                return await r.json()

    async def chat(self, workspace_id: str, prompt: str) -> dict:
        """POST /v1/chat — Phase 3.5 才會通。目前 sidecar 回 501,client 把錯誤
        包成 OpenClawError 讓 caller(ai_chat router)能 graceful fallback 回
        Hermes 或回友善訊息。"""
        url = f"{self.base_url}/v1/chat"
        async with aiohttp.ClientSession(timeout=self.timeout) as session:
            async with session.post(
                url,
                headers=self._headers(),
                json={"workspace_id": workspace_id, "prompt": prompt},
            ) as r:
                body_text = await r.text()
                if r.status == 501:
                    raise OpenClawError(
                        "openclaw_chat_not_yet_wired: " + body_text[:200]
                    )
                if r.status >= 400:
                    raise OpenClawError(f"chat: HTTP {r.status} — {body_text[:200]}")
                return await r.json()


# Singleton(對齊 hermes_client 的 _global_hermes_client pattern)
_GLOBAL_CLIENT: Optional[OpenClawClient] = None


def get_openclaw_client() -> OpenClawClient:
    global _GLOBAL_CLIENT
    if _GLOBAL_CLIENT is None:
        _GLOBAL_CLIENT = OpenClawClient()
    return _GLOBAL_CLIENT
