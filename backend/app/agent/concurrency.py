"""Per-user concurrency limit for agent tools — 防止 LLM 在 loop 內狂派 tool。

對應風險紅線:VM 磁碟容量 + robot-runner 容器數量。LLM 一不小心會在
tool-use 迴圈內反覆呼叫同一 tool;這層用 Valkey INCR + TTL 計每個 user
in-flight 多少個 slot,逾上限直接拒派,LLM 看到 fail message 會自然收手。

設計:
* Key: ``agent:concur:{user_id}:{tool_name}``
* TTL: 預設 30 分鐘(robot 一次 run 的合理上限);時間到自動釋放,即使
  Celery worker 沒主動 release 也不會永久卡住 slot。
* Fail-open:Redis 不通時放行(沿用 [[revocation.py]] 的設計哲學 — 基礎
  設施故障不該整個系統當機)。同時 log warning 讓 ops 知道要修。
* Tool 失敗時主動 release;非同步 tool 派 Celery 成功則不 release,
  讓 TTL 自然到期或由 worker 跑完事件回流時釋放(Phase 1c-2)。

不在這層做的事:
* 「全 org 上限」(Phase 1c-2+):目前只看 per-user,reasonable for single-tenant
  RL 部署。多租戶 SaaS 場景才需要 org-level 計數
* 「全平台容器上限」:那是 docker-compose / kubernetes 資源限制的事
"""
from __future__ import annotations

import logging
from typing import Optional

from app.config import settings

log = logging.getLogger(__name__)

# 預設 TTL — 30 分鐘。robot 一次 run 上限通常不到這個數,且 TTL 兼做「safety net」
# 防止 worker 跑完沒 release 時 slot 永久卡住。
DEFAULT_TTL_SEC = 1800

# Lazy 建立 redis client;沿用既有 revocation.py 的單例模式
_async_redis = None


async def _get_redis():
    global _async_redis
    if _async_redis is None:
        from redis import asyncio as aioredis

        _async_redis = aioredis.from_url(
            settings.REDIS_URL, encoding="utf-8", decode_responses=True
        )
    return _async_redis


def _key(user_id: str, tool_name: str) -> str:
    return f"agent:concur:{user_id}:{tool_name}"


async def try_acquire(
    user_id: str, tool_name: str, *, limit: int, ttl_sec: int = DEFAULT_TTL_SEC
) -> tuple[bool, int]:
    """嘗試佔一個 slot。

    Returns (acquired, current_count)。acquired=True 表示成功佔到一個,呼叫端
    可以繼續 dispatch tool。acquired=False 表示已達上限,呼叫端應 raise / 回
    fail。

    Fail-open:Redis 連不上時回 ``(True, 0)`` — 不擋 user,但 ops 應從 log 看到。
    """
    if limit <= 0:
        return False, 0
    try:
        client = await _get_redis()
        key = _key(user_id, tool_name)
        # Atomic INCR + EXPIRE(EXPIRE 每次重設,確保 slot 不會在 TTL 邊界丟失)
        async with client.pipeline(transaction=True) as pipe:
            pipe.incr(key)
            pipe.expire(key, ttl_sec)
            results = await pipe.execute()
        count = int(results[0])
        if count > limit:
            # 超額 — DECR 撤回,讓計數準確
            try:
                await client.decr(key)
            except Exception:  # noqa: BLE001
                # decr 失敗也只是計數高 1,TTL 過後自動修正
                pass
            return False, count - 1
        return True, count
    except Exception as e:  # noqa: BLE001 — fail-open
        log.warning(
            "concurrency try_acquire failed for user=%s tool=%s (Valkey down?): %s; "
            "allowing fail-open",
            user_id,
            tool_name,
            e,
        )
        return True, 0


async def release(user_id: str, tool_name: str) -> None:
    """釋放一個 slot。Tool 失敗 / Celery worker 完成時呼叫。

    Fail-open:Redis 不通就 log + 走;TTL 自然到期會修正。
    """
    try:
        client = await _get_redis()
        key = _key(user_id, tool_name)
        new_count = await client.decr(key)
        # 計數歸零或負就刪 key,避免長時間留著
        if new_count is not None and new_count <= 0:
            try:
                await client.delete(key)
            except Exception:  # noqa: BLE001
                pass
    except Exception as e:  # noqa: BLE001
        log.warning(
            "concurrency release failed for user=%s tool=%s: %s",
            user_id,
            tool_name,
            e,
        )


async def get_current_count(user_id: str, tool_name: str) -> Optional[int]:
    """查目前佔用數;Redis 壞回 None(caller 自行決定是否顯示)。"""
    try:
        client = await _get_redis()
        val = await client.get(_key(user_id, tool_name))
        return int(val) if val is not None else 0
    except Exception:  # noqa: BLE001
        return None
