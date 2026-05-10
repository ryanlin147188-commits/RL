"""Hermes sidecar HTTP client。

PR2 範圍:provision/sessions/messages 三類呼叫。後續 PR 會擴 skills/cron/gateway/memory。

設計:
- 抽象 base class 讓 router 可以 swap 實作(PR3 加 stream client、null fallback 等)
- 預設 HTTP impl 走 internal docker network、X-Sidecar-Auth header
- 失敗一律包成 HermesUnavailable(網路 / sidecar 503)或 HermesAcpError
  (sidecar 200 但 ACP 子進程錯誤),由呼叫端決定怎麼回 user(503/502/400)
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Optional

import httpx

from app.config import settings


# ── Exceptions ─────────────────────────────────────────────────────
class HermesError(Exception):
    """Hermes client 統一錯誤型別 base。"""


class HermesUnavailable(HermesError):
    """Sidecar 連不上 / 5xx / timeout — 應視為服務暫時不可用,回 503。"""


class HermesAuthFailed(HermesError):
    """Sidecar 回 401 — 通常代表 SIDECAR_AUTH_TOKEN 錯,代表 misconfig。"""


class HermesAcpError(HermesError):
    """Sidecar 回 502 帶 ACP 子進程錯誤 — 上游 LLM 或 provision 問題,回 502/400。"""

    def __init__(self, code: int, detail: str):
        self.code = code
        self.detail = detail
        super().__init__(f"ACP error {code}: {detail}")


class HermesBadRequest(HermesError):
    """Sidecar 回 400 — payload 無效,通常是程式 bug。"""


class HermesNotFound(HermesError):
    """Sidecar 回 404 — resource(如 cron job_id)不存在。"""


# ── Abstract base ──────────────────────────────────────────────────
class HermesClient(ABC):
    @abstractmethod
    async def healthcheck(self) -> bool: ...

    @abstractmethod
    async def provision(
        self,
        workspace_id: str,
        provider: str,
        api_key: str,
        base_url: Optional[str] = None,
        system_prompt: Optional[str] = None,
    ) -> None: ...

    @abstractmethod
    async def create_session(
        self,
        workspace_id: str,
        mcp_servers: Optional[list[dict]] = None,
    ) -> dict: ...

    @abstractmethod
    async def list_sessions(self, workspace_id: str) -> dict: ...

    @abstractmethod
    async def send_message(
        self, workspace_id: str, session_id: str, content: str,
    ) -> dict: ...

    @abstractmethod
    async def list_skills(self, workspace_id: str) -> dict: ...

    @abstractmethod
    async def search_memory(
        self, workspace_id: str, query: str, limit: int = 20,
    ) -> dict: ...

    @abstractmethod
    async def list_cron_jobs(self, workspace_id: str) -> dict: ...

    @abstractmethod
    async def add_cron_job(
        self, workspace_id: str, *, schedule: str, prompt: str,
        name: Optional[str] = None,
    ) -> dict: ...

    @abstractmethod
    async def delete_cron_job(self, workspace_id: str, job_id: str) -> None: ...

    @abstractmethod
    async def gateway_status(self, workspace_id: str) -> dict: ...

    @abstractmethod
    async def gateway_enable(
        self, workspace_id: str, platform: str, *, token: str,
        extra: Optional[dict] = None,
    ) -> dict: ...

    @abstractmethod
    async def gateway_disable(self, workspace_id: str, platform: str) -> None: ...

    async def aclose(self) -> None:
        """子類覆寫做 cleanup。Default: no-op。"""


# ── HTTP impl ──────────────────────────────────────────────────────
class HermesHttpClient(HermesClient):
    """呼叫 hermes sidecar(http://hermes:7800)的真實 client。"""

    def __init__(
        self,
        base_url: Optional[str] = None,
        auth_token: Optional[str] = None,
        timeout: Optional[float] = None,
    ):
        self._base_url = (base_url or settings.HERMES_BASE_URL).rstrip("/")
        self._auth_token = auth_token if auth_token is not None else settings.SIDECAR_AUTH_TOKEN
        self._timeout = timeout if timeout is not None else float(settings.HERMES_TIMEOUT_SEC)
        # 連線池 share 給整個 backend process 用 — FastAPI lifespan 結束才 aclose
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=self._timeout,
            headers={"X-Sidecar-Auth": self._auth_token} if self._auth_token else {},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # ── 內部:統一 request + 錯誤映射 ─────────────────────────────
    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[dict] = None,
        params: Optional[dict] = None,
        timeout: Optional[float] = None,
        skip_auth: bool = False,
    ) -> httpx.Response:
        try:
            resp = await self._client.request(
                method,
                path,
                json=json_body,
                params=params,
                timeout=timeout if timeout is not None else self._timeout,
                # healthz 不需 auth — 對 healthz 移除 header 避免 sidecar 認證錯誤干擾診斷
                headers={"X-Sidecar-Auth": ""} if skip_auth else None,
            )
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout) as e:
            raise HermesUnavailable(f"sidecar_unreachable:{type(e).__name__}") from e
        except httpx.HTTPError as e:
            raise HermesUnavailable(f"http_error:{type(e).__name__}") from e
        if resp.status_code == 401:
            raise HermesAuthFailed("sidecar rejected X-Sidecar-Auth (config mismatch)")
        if resp.status_code == 400:
            try:
                detail = resp.json().get("detail") or resp.text
            except Exception:
                detail = resp.text
            raise HermesBadRequest(f"sidecar_400: {detail}")
        if resp.status_code == 404:
            raise HermesNotFound(f"sidecar_404: {resp.text[:120] or 'not_found'}")
        if resp.status_code == 502:
            try:
                body = resp.json()
            except Exception:
                body = {}
            raise HermesAcpError(
                int(body.get("code") or -32000),
                str(body.get("detail") or resp.text),
            )
        if resp.status_code >= 500 or resp.status_code == 504:
            raise HermesUnavailable(f"sidecar_{resp.status_code}: {resp.text[:200]}")
        if resp.status_code >= 400:
            raise HermesError(f"unexpected_status:{resp.status_code}: {resp.text[:200]}")
        return resp

    # ── 公開 API ──────────────────────────────────────────────────
    async def healthcheck(self) -> bool:
        if not settings.HERMES_ENABLED:
            return False
        try:
            resp = await self._request("GET", "/healthz", timeout=2.0, skip_auth=True)
            return resp.status_code == 200 and (resp.json().get("status") == "ok")
        except HermesError:
            return False

    async def provision(
        self,
        workspace_id: str,
        provider: str,
        api_key: str,
        base_url: Optional[str] = None,
        system_prompt: Optional[str] = None,
        model: Optional[str] = None,
    ) -> None:
        body: dict[str, Any] = {"provider": provider, "api_key": api_key}
        if base_url:
            body["base_url"] = base_url
        if system_prompt:
            body["system_prompt"] = system_prompt
        if model:
            # 顯式落 model.default 進 sidecar config.yaml,避免 Hermes 預設用 openrouter。
            body["model"] = model
        await self._request(
            "POST",
            f"/admin/users/{workspace_id}/provision",
            json_body=body,
        )

    async def create_session(
        self,
        workspace_id: str,
        mcp_servers: Optional[list[dict]] = None,
    ) -> dict:
        """建立 ACP session,可選地把 MCP server config 帶給 hermes 註冊成 LLM tool。

        mcp_servers schema(supervisor 端 _normalize_mcp_server 補 ACP 必填欄位):
          [{"name": "memory", "url": "http://mem0:7900/mcp/mcp",
            "headers": [{"name": "X-Sidecar-Auth", "value": "..."},
                        {"name": "X-Mem0-User-Id", "value": "..."}]}]

        None / [] 都不帶 mcpServers field — 對齊舊行為。
        """
        body: dict[str, Any] = {}
        if mcp_servers:
            body["mcp_servers"] = mcp_servers
        resp = await self._request(
            "POST",
            f"/v1/workspaces/{workspace_id}/sessions",
            json_body=body,
            # ACP cold start + initialize + authenticate 可能要 30-60s,給滿
            timeout=float(settings.HERMES_TIMEOUT_SEC),
        )
        return resp.json()

    async def list_sessions(self, workspace_id: str) -> dict:
        resp = await self._request("GET", f"/v1/workspaces/{workspace_id}/sessions")
        return resp.json()

    async def send_message(
        self, workspace_id: str, session_id: str, content: str,
    ) -> dict:
        resp = await self._request(
            "POST",
            f"/v1/workspaces/{workspace_id}/sessions/{session_id}/messages",
            json_body={"content": content},
            # 大模型回覆有時超過 60s — 給到 stream timeout 上限
            timeout=float(settings.HERMES_STREAM_TIMEOUT_SEC),
        )
        return resp.json()

    async def list_skills(self, workspace_id: str) -> dict:
        # 純檔案系統 walk,sidecar 端不開 ACP 子進程,所以不該需要長 timeout
        resp = await self._request(
            "GET",
            f"/v1/workspaces/{workspace_id}/skills",
            timeout=10.0,
        )
        return resp.json()

    async def search_memory(
        self, workspace_id: str, query: str, limit: int = 20,
    ) -> dict:
        # SQLite read-only FTS5 — 也很快,不該超過幾百 ms
        resp = await self._request(
            "GET",
            f"/v1/workspaces/{workspace_id}/memory/search",
            timeout=10.0,
            params={"q": query, "limit": str(limit)},
        )
        return resp.json()

    async def list_cron_jobs(self, workspace_id: str) -> dict:
        resp = await self._request(
            "GET",
            f"/v1/workspaces/{workspace_id}/cron",
            timeout=10.0,
        )
        return resp.json()

    async def add_cron_job(
        self, workspace_id: str, *, schedule: str, prompt: str,
        name: Optional[str] = None,
    ) -> dict:
        body: dict[str, Any] = {"schedule": schedule, "prompt": prompt}
        if name:
            body["name"] = name
        resp = await self._request(
            "POST",
            f"/v1/workspaces/{workspace_id}/cron",
            json_body=body,
            timeout=10.0,
        )
        return resp.json()

    async def delete_cron_job(self, workspace_id: str, job_id: str) -> None:
        await self._request(
            "DELETE",
            f"/v1/workspaces/{workspace_id}/cron/{job_id}",
            timeout=10.0,
        )

    async def gateway_status(self, workspace_id: str) -> dict:
        resp = await self._request(
            "GET",
            f"/v1/workspaces/{workspace_id}/gateway",
            timeout=10.0,
        )
        return resp.json()

    async def gateway_enable(
        self, workspace_id: str, platform: str, *, token: str,
        extra: Optional[dict] = None,
    ) -> dict:
        body: dict[str, Any] = {"token": token}
        if extra is not None:
            body["extra"] = extra
        # Spawn gateway.run cold-start 可能要 20s+(load deps + connect Telegram)
        resp = await self._request(
            "POST",
            f"/v1/workspaces/{workspace_id}/gateway/{platform}/enable",
            json_body=body,
            timeout=30.0,
        )
        return resp.json()

    async def gateway_disable(self, workspace_id: str, platform: str) -> None:
        await self._request(
            "POST",
            f"/v1/workspaces/{workspace_id}/gateway/{platform}/disable",
            timeout=15.0,
        )


# ── Factory ────────────────────────────────────────────────────────
_singleton: Optional[HermesClient] = None


def get_hermes_client() -> HermesClient:
    """FastAPI dependency / Celery context 都從這裡取。

    Single shared httpx connection pool;backend lifespan 結束才 aclose。
    """
    global _singleton
    if _singleton is None:
        _singleton = HermesHttpClient()
    return _singleton


async def close_hermes_client() -> None:
    """FastAPI lifespan shutdown 呼叫一次,關連線池。"""
    global _singleton
    if _singleton is not None:
        await _singleton.aclose()
        _singleton = None
