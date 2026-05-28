"""帳號層級登入失敗節流(用 Redis 做 counter,不必改 DB schema)。

slowapi 的 IP-based rate limit 防不住分散式 botnet(每 IP 各打幾次就過了)。
這層做的是「**對某個 username 的失敗計數**」,不論來源 IP:
* 連續 N 次失敗在 W 內 → 鎖該 username 共 L 秒
* 任何登入成功會清掉 counter
* 鎖住期間不論輸入是否正確都直接 reject

預設:N=8 次失敗 → 鎖 5 分鐘;可由環境變數覆蓋。
"""
from __future__ import annotations

import logging
import os
from typing import Optional

log = logging.getLogger(__name__)

_THRESHOLD = int(os.environ.get("AUTOTEST_LOGIN_FAIL_THRESHOLD", "8"))
_WINDOW_SEC = int(os.environ.get("AUTOTEST_LOGIN_FAIL_WINDOW_SEC", "900"))     # 15 min counter TTL
_LOCK_SEC = int(os.environ.get("AUTOTEST_LOGIN_LOCK_SEC", "300"))              # 5 min lock


def _key_counter(username: str) -> str:
    return f"login_fail:{(username or '').strip().lower()}:count"


def _key_lock(username: str) -> str:
    return f"login_fail:{(username or '').strip().lower()}:lock"


async def _redis():
    """重用 revocation 模組的 Redis client(同一條 URL,共用 pool)。"""
    from app.auth.revocation import _get_async_redis
    return await _get_async_redis()


async def is_locked(username: str) -> Optional[int]:
    """回傳剩餘鎖定秒數(>0 表示鎖中);None / 0 = 未鎖。"""
    if not username:
        return None
    try:
        r = await _redis()
        ttl = await r.ttl(_key_lock(username))
    except Exception as e:  # noqa: BLE001
        log.debug("login_throttle is_locked redis error: %s", e)
        return None
    return ttl if ttl and ttl > 0 else None


async def record_failure(username: str) -> Optional[int]:
    """登入失敗時呼叫;失敗超過閾值就上鎖。回傳剩餘鎖定秒數(若觸發鎖)。"""
    if not username:
        return None
    try:
        r = await _redis()
        ck = _key_counter(username)
        n = await r.incr(ck)
        if n == 1:
            await r.expire(ck, _WINDOW_SEC)
        if n >= _THRESHOLD:
            lk = _key_lock(username)
            await r.set(lk, "1", ex=_LOCK_SEC)
            # 清 counter 避免下次解鎖後第 1 次失敗又立刻觸發
            await r.delete(ck)
            log.warning(
                "login_throttle: locking username=%s for %ds after %d failures",
                username, _LOCK_SEC, n,
            )
            return _LOCK_SEC
    except Exception as e:  # noqa: BLE001
        log.debug("login_throttle record_failure redis error: %s", e)
    return None


async def clear_on_success(username: str) -> None:
    """登入成功時清掉 counter + lock。"""
    if not username:
        return
    try:
        r = await _redis()
        await r.delete(_key_counter(username), _key_lock(username))
    except Exception as e:  # noqa: BLE001
        log.debug("login_throttle clear redis error: %s", e)
