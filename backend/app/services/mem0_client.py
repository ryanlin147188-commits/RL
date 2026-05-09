"""mem0 sidecar HTTP client。

PR3 範圍:add/search/list/delete/delete_all 6 個 method + circuit breaker +
graceful degradation。`*_safe` 系列在 mem0 故障時不 raise(只回 None / [] /
False),讓上游 send_message 主流程不受 mem0 影響(plan §6)。

設計:
- httpx.AsyncClient 連線池,backend lifespan 結束才 aclose
- Circuit breaker:連續失敗 5 次就 60 秒不嘗試,避免 timeout 雪崩
- timeout:add/list/delete 5s、search 3s(plan §6 graceful degradation)
- LLM key 永遠經 backend 解 Fernet 後 plaintext 推給 sidecar
  (走 docker network + X-Sidecar-Auth)
"""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

import httpx

from app.config import settings

LOG = logging.getLogger(__name__)


# ── Exceptions(PR3:呼叫端可選 raise 或走 _safe 路徑)────────────
class Mem0Error(Exception):
    """Mem0 client 統一 base — backend 不該 raise 這個給 user(graceful degrade)。"""


class Mem0Unavailable(Mem0Error):
    """Sidecar 連不上 / 5xx / timeout — 視為服務暫時不可用。"""


class Mem0AuthFailed(Mem0Error):
    """Sidecar 回 401 — backend ↔ sidecar config mismatch,operations issue。"""


class Mem0BadRequest(Mem0Error):
    """Sidecar 回 400/422 — payload 無效,通常是程式 bug。"""


class Mem0NotFound(Mem0Error):
    """Sidecar 回 404 — memory_id 不存在或 ownership 不對。"""


class Mem0ProviderError(Mem0Error):
    """Sidecar 回 502 — mem0 lib 內 LLM provider 拒絕(invalid key、quota)。"""


# ── Circuit breaker(thread-safe enough for asyncio)──────────────
class _CircuitBreaker:
    """連續失敗 N 次就 cool_down 秒不嘗試,避免每 request 都打 timeout 雪崩。"""

    def __init__(self, failure_threshold: int = 5, cool_down_sec: float = 60.0):
        self._fail_count = 0
        self._tripped_at: Optional[float] = None
        self.threshold = failure_threshold
        self.cool_down = cool_down_sec

    def is_open(self) -> bool:
        """True = 跳閘中,別呼叫,直接 fail-fast。"""
        if self._tripped_at is None:
            return False
        if time.monotonic() - self._tripped_at >= self.cool_down:
            # cool_down 過了,重置半開
            self._tripped_at = None
            self._fail_count = 0
            return False
        return True

    def record_success(self) -> None:
        self._fail_count = 0
        self._tripped_at = None

    def record_failure(self) -> None:
        self._fail_count += 1
        if self._fail_count >= self.threshold and self._tripped_at is None:
            self._tripped_at = time.monotonic()
            LOG.warning(
                "mem0 circuit breaker OPEN — %d consecutive failures, "
                "cooling down %ds",
                self._fail_count, self.cool_down,
            )


# ── Mem0Client(實作)───────────────────────────────────────────────
class Mem0Client:
    def __init__(
        self,
        base_url: Optional[str] = None,
        auth_token: Optional[str] = None,
        timeout: Optional[float] = None,
        search_timeout: Optional[float] = None,
    ):
        self._base_url = (base_url or settings.MEM0_BASE_URL).rstrip("/")
        self._auth = auth_token if auth_token is not None else settings.MEM0_SIDECAR_AUTH_TOKEN
        self._timeout = timeout if timeout is not None else float(settings.MEM0_TIMEOUT_SEC)
        self._search_timeout = (
            search_timeout if search_timeout is not None
            else float(settings.MEM0_SEARCH_TIMEOUT_SEC)
        )
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=self._timeout,
            headers={"X-Sidecar-Auth": self._auth} if self._auth else {},
        )
        self._breaker = _CircuitBreaker(failure_threshold=5, cool_down_sec=60.0)

    async def aclose(self) -> None:
        await self._client.aclose()

    # ── 內部統一 request ────────────────────────────────────────
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
        if self._breaker.is_open():
            raise Mem0Unavailable("circuit_breaker_open")
        try:
            resp = await self._client.request(
                method, path,
                json=json_body, params=params,
                timeout=timeout if timeout is not None else self._timeout,
                headers={"X-Sidecar-Auth": ""} if skip_auth else None,
            )
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as e:
            self._breaker.record_failure()
            raise Mem0Unavailable(f"sidecar_unreachable:{type(e).__name__}") from e
        except httpx.HTTPError as e:
            self._breaker.record_failure()
            raise Mem0Unavailable(f"http_error:{type(e).__name__}") from e

        # 5xx 也算 breaker failure
        if resp.status_code >= 500:
            self._breaker.record_failure()
        else:
            self._breaker.record_success()

        if resp.status_code == 401:
            raise Mem0AuthFailed("sidecar rejected X-Sidecar-Auth (config mismatch)")
        if resp.status_code in (400, 422):
            try:
                detail = resp.json().get("detail") or resp.text
            except Exception:
                detail = resp.text
            raise Mem0BadRequest(f"sidecar_{resp.status_code}: {detail}")
        if resp.status_code == 404:
            raise Mem0NotFound(f"sidecar_404: {resp.text[:120] or 'not_found'}")
        if resp.status_code == 502:
            try:
                detail = resp.json().get("detail") or resp.text
            except Exception:
                detail = resp.text
            raise Mem0ProviderError(f"sidecar_502: {detail}")
        if resp.status_code >= 500 or resp.status_code == 504:
            raise Mem0Unavailable(f"sidecar_{resp.status_code}: {resp.text[:200]}")
        if resp.status_code >= 400:
            raise Mem0Error(f"unexpected_status:{resp.status_code}: {resp.text[:200]}")
        return resp

    # ── 公開 API:raising ────────────────────────────────────────
    async def healthcheck(self) -> bool:
        if not settings.MEM0_ENABLED:
            return False
        try:
            resp = await self._request("GET", "/healthz", timeout=2.0, skip_auth=True)
            return resp.status_code == 200 and (resp.json().get("status") == "ok")
        except Mem0Error:
            return False

    async def add(
        self,
        user_id: str,
        messages: Any,
        llm_config: dict,
        embedder_config: dict,
        metadata: Optional[dict] = None,
        infer: bool = True,
    ) -> dict:
        body: dict[str, Any] = {
            "user_id": user_id,
            "messages": messages,
            "llm_config": llm_config,
            "embedder_config": embedder_config,
            "infer": infer,
        }
        if metadata is not None:
            body["metadata"] = metadata
        # mem0.add 內部會 LLM call(可能 1-2s),給足 timeout
        resp = await self._request("POST", "/v1/memory/add", json_body=body, timeout=15.0)
        return resp.json()

    async def search(
        self,
        user_id: str,
        query: str,
        llm_config: dict,
        embedder_config: dict,
        top_k: int = 5,
        threshold: Optional[float] = 0.3,
    ) -> list[dict]:
        body: dict[str, Any] = {
            "user_id": user_id,
            "query": query,
            "llm_config": llm_config,
            "embedder_config": embedder_config,
            "top_k": top_k,
            "threshold": threshold,
        }
        resp = await self._request(
            "POST", "/v1/memory/search",
            json_body=body, timeout=self._search_timeout,
        )
        return (resp.json() or {}).get("results") or []

    async def list_memories(
        self,
        user_id: str,
        llm_config: dict,
        embedder_config: dict,
        limit: int = 50,
    ) -> list[dict]:
        body = {
            "user_id": user_id,
            "llm_config": llm_config,
            "embedder_config": embedder_config,
            "limit": limit,
        }
        resp = await self._request("POST", "/v1/memory/list", json_body=body)
        return (resp.json() or {}).get("results") or []

    async def delete_memory(
        self,
        user_id: str,
        memory_id: str,
        llm_config: dict,
        embedder_config: dict,
    ) -> None:
        body = {
            "user_id": user_id,
            "llm_config": llm_config,
            "embedder_config": embedder_config,
        }
        await self._request("DELETE", f"/v1/memory/{memory_id}", json_body=body)

    async def delete_all(
        self,
        user_id: str,
        llm_config: dict,
        embedder_config: dict,
    ) -> None:
        body = {
            "user_id": user_id,
            "llm_config": llm_config,
            "embedder_config": embedder_config,
            "confirm": True,
        }
        await self._request("DELETE", "/v1/memory/all", json_body=body)

    # ── Per-user LLM config push/clear(PR3 — Hermes MCP tool 用 cache)──
    # mem0 sidecar 在 /admin/users/{user_id}/llm_config 上維護一份 5min TTL 的
    # cache。當 Hermes ACP 子進程的 LLM 透過 MCP `search_memory` tool 進來時,
    # tool function 從 ContextVar 拿到 user_id 後直接用 cache 裡的 config 跑
    # mem0.search — LLM key 永不流經 MCP layer 的 tool args 或 Hermes context。
    async def push_llm_config(
        self,
        user_id: str,
        llm_config: dict,
        embedder_config: dict,
    ) -> None:
        """寫入 mem0 sidecar per-user llm_config cache。

        backend 在 ensure_user_workspace 與 settings token 換新時呼叫。
        失敗 raise Mem0Error,上層決定是 log warning(graceful)或 propagate。
        """
        await self._request(
            "POST",
            f"/admin/users/{user_id}/llm_config",
            json_body={
                "llm_config": llm_config,
                "embedder_config": embedder_config,
            },
            timeout=5.0,
        )

    async def clear_llm_config(self, user_id: str) -> None:
        """清掉 sidecar 對該 user 的 cache(刪 token 時用)。

        sidecar 對「沒 cache 的 user」delete 也回 204(idempotent)。失敗 raise
        Mem0Error 由上層處理。
        """
        await self._request(
            "DELETE",
            f"/admin/users/{user_id}/llm_config",
            timeout=5.0,
        )

    # ── 公開 API:safe wrappers(graceful degrade,plan §6)──────
    async def add_safe(
        self,
        user_id: str,
        messages: Any,
        llm_config: dict,
        embedder_config: dict,
        metadata: Optional[dict] = None,
    ) -> bool:
        """fire-and-forget add — 失敗只 log 不 raise。

        這是 send_message post-hook 用的;mem0 故障絕不能 cascade 到主對話流程。
        回 True/False 表示成功與否,呼叫端通常忽略。
        """
        if not settings.MEM0_ENABLED:
            return False
        try:
            await self.add(
                user_id=user_id, messages=messages,
                llm_config=llm_config, embedder_config=embedder_config,
                metadata=metadata,
            )
            return True
        except Mem0Error as e:
            LOG.warning("mem0 add_safe failed user=%s: %s", user_id, e)
            return False
        except Exception:  # noqa: BLE001
            LOG.exception("mem0 add_safe unexpected error user=%s", user_id)
            return False

    async def search_safe(
        self,
        user_id: str,
        query: str,
        llm_config: dict,
        embedder_config: dict,
        top_k: int = 5,
        threshold: Optional[float] = 0.3,
    ) -> list[dict]:
        """search 失敗回空 list — 讓 pre-hook(PR6)無記憶時走原 prompt。"""
        if not settings.MEM0_ENABLED:
            return []
        try:
            return await self.search(
                user_id=user_id, query=query,
                llm_config=llm_config, embedder_config=embedder_config,
                top_k=top_k, threshold=threshold,
            )
        except Mem0Error as e:
            LOG.warning("mem0 search_safe failed user=%s: %s", user_id, e)
            return []
        except Exception:  # noqa: BLE001
            LOG.exception("mem0 search_safe unexpected error user=%s", user_id)
            return []

    async def push_llm_config_safe(
        self,
        user_id: str,
        llm_config: dict,
        embedder_config: dict,
    ) -> bool:
        """ensure_user_workspace + settings token rotate 用的 fire-and-forget 路徑。

        sidecar push 失敗不該擋使用者建 session — 只 log warning,後續使用者
        invoke MCP tool 時會拿到 friendly error("memory unavailable"),自然繼續對話。
        """
        if not settings.MEM0_ENABLED:
            return False
        try:
            await self.push_llm_config(user_id, llm_config, embedder_config)
            return True
        except Mem0Error as e:
            LOG.warning("mem0 push_llm_config_safe failed user=%s: %s", user_id, e)
            return False
        except Exception:  # noqa: BLE001
            LOG.exception("mem0 push_llm_config_safe unexpected error user=%s", user_id)
            return False

    async def clear_llm_config_safe(self, user_id: str) -> bool:
        """delete_ai_token 用的 fire-and-forget — 失敗只 log;cache 5min 後也會自然過期。"""
        if not settings.MEM0_ENABLED:
            return False
        try:
            await self.clear_llm_config(user_id)
            return True
        except Mem0Error as e:
            LOG.warning("mem0 clear_llm_config_safe failed user=%s: %s", user_id, e)
            return False
        except Exception:  # noqa: BLE001
            LOG.exception("mem0 clear_llm_config_safe unexpected error user=%s", user_id)
            return False


# ── Singleton ───────────────────────────────────────────────────────
_singleton: Optional[Mem0Client] = None


def get_mem0_client() -> Mem0Client:
    """FastAPI dependency / Celery context 都從這裡取。

    Single shared httpx connection pool;backend lifespan shutdown 才 aclose。
    """
    global _singleton
    if _singleton is None:
        _singleton = Mem0Client()
    return _singleton


async def close_mem0_client() -> None:
    """FastAPI lifespan shutdown 呼叫一次,關連線池。"""
    global _singleton
    if _singleton is not None:
        await _singleton.aclose()
        _singleton = None
