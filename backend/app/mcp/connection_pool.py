"""MCP connection pool — 進程級 cache,避免每次 call_tool 都重連。

策略:
* key = ``server_id``(per-org 隔離由 service 層在 list 階段做掉)
* 每個 server 一個 ``MCPClient``;首次 call 時 lazy 起連線
* idle 超過 60 秒沒人用 → close(避免 stdio 殭屍 / HTTP keep-alive 佔資源)
* per-server 並發上限 = 2(防 LLM 在 tool-use loop 內爆送同一 server)
* call_tool 30 秒 timeout

執行緒安全:FastAPI / uvicorn 是單一 event loop;不需要 threading lock,
asyncio.Lock 已足夠保護 pool 內部結構。

呼叫者協議:
* 走 ``async with pool.acquire(...)`` 拿 client → call_tool → ctx 結束自動釋放
  in-flight 計數。client 物件本身留在 pool 內等下一次取用。
"""
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

from .client import MCPClient, MCPConnectionError

log = logging.getLogger(__name__)


# 預設參數;測試可以 monkeypatch 改小
IDLE_GC_SECONDS = 60.0
CALL_TIMEOUT_SECONDS = 30.0
PER_SERVER_MAX_IN_FLIGHT = 2


class _PoolEntry:
    __slots__ = ("client", "last_used_at", "semaphore")

    def __init__(self, client: MCPClient):
        self.client = client
        self.last_used_at = time.monotonic()
        self.semaphore = asyncio.Semaphore(PER_SERVER_MAX_IN_FLIGHT)


class MCPConnectionPool:
    def __init__(self) -> None:
        self._entries: dict[str, _PoolEntry] = {}
        self._lock = asyncio.Lock()
        self._gc_task: Optional[asyncio.Task] = None

    async def _ensure_gc(self) -> None:
        """背景 task GC idle 連線。Lazy start — 第一次有人 acquire 才開。"""
        if self._gc_task is not None and not self._gc_task.done():
            return
        loop = asyncio.get_running_loop()
        self._gc_task = loop.create_task(self._gc_loop(), name="mcp-pool-gc")

    async def _gc_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(IDLE_GC_SECONDS / 2)
                await self._gc_idle()
        except asyncio.CancelledError:
            return

    async def _gc_idle(self) -> None:
        now = time.monotonic()
        to_close: list[tuple[str, MCPClient]] = []
        async with self._lock:
            for sid, entry in list(self._entries.items()):
                # in-flight 中(semaphore 內計數 != 上限)→ 不 GC
                in_flight = PER_SERVER_MAX_IN_FLIGHT - entry.semaphore._value
                if in_flight > 0:
                    continue
                if now - entry.last_used_at >= IDLE_GC_SECONDS:
                    to_close.append((sid, entry.client))
                    del self._entries[sid]
        for sid, client in to_close:
            log.info("MCP pool: closing idle connection server_id=%s", sid)
            try:
                await client.close()
            except Exception:  # noqa: BLE001
                pass

    async def _get_or_open(
        self,
        *,
        server_id: str,
        transport: str,
        url: Optional[str],
        headers: Optional[dict[str, str]],
        command: Optional[str],
        args: Optional[list[str]],
        env: Optional[dict[str, str]],
    ) -> _PoolEntry:
        async with self._lock:
            entry = self._entries.get(server_id)
            if entry is not None:
                entry.last_used_at = time.monotonic()
                return entry
        # release lock before await(避免 lock 整個連線時間)
        client = await MCPClient.open(
            server_id=server_id,
            transport=transport,
            url=url,
            headers=headers,
            command=command,
            args=args,
            env=env,
        )
        async with self._lock:
            # double-check:可能別人也剛建好
            existing = self._entries.get(server_id)
            if existing is not None:
                # 別人建好了,把我這條關掉
                async def _bg_close():
                    try:
                        await client.close()
                    except Exception:  # noqa: BLE001
                        pass
                asyncio.create_task(_bg_close())
                return existing
            entry = _PoolEntry(client)
            self._entries[server_id] = entry
            return entry

    @asynccontextmanager
    async def acquire(
        self,
        *,
        server_id: str,
        transport: str,
        url: Optional[str] = None,
        headers: Optional[dict[str, str]] = None,
        command: Optional[str] = None,
        args: Optional[list[str]] = None,
        env: Optional[dict[str, str]] = None,
    ) -> AsyncIterator[MCPClient]:
        """取一條連到 server 的 client(per-server 並發控制由 semaphore 把關)。"""
        await self._ensure_gc()
        entry = await self._get_or_open(
            server_id=server_id,
            transport=transport,
            url=url,
            headers=headers,
            command=command,
            args=args,
            env=env,
        )
        await entry.semaphore.acquire()
        try:
            entry.last_used_at = time.monotonic()
            yield entry.client
            entry.last_used_at = time.monotonic()
        except MCPConnectionError:
            # 連線壞了 → 移出 pool 強制下次重連
            async with self._lock:
                if self._entries.get(server_id) is entry:
                    del self._entries[server_id]
            try:
                await entry.client.close()
            except Exception:  # noqa: BLE001
                pass
            raise
        finally:
            entry.semaphore.release()

    async def invalidate(self, server_id: str) -> None:
        """強制關掉某 server 的連線(config 改動 / test 失敗時呼叫)。"""
        async with self._lock:
            entry = self._entries.pop(server_id, None)
        if entry is not None:
            try:
                await entry.client.close()
            except Exception:  # noqa: BLE001
                pass

    async def close_all(self) -> None:
        """關掉所有連線(用於 lifespan shutdown)。"""
        async with self._lock:
            entries = list(self._entries.values())
            self._entries.clear()
        for entry in entries:
            try:
                await entry.client.close()
            except Exception:  # noqa: BLE001
                pass
        if self._gc_task is not None and not self._gc_task.done():
            self._gc_task.cancel()


# 進程級 singleton — agent_service 與 mcp_server_service 共用
POOL = MCPConnectionPool()
