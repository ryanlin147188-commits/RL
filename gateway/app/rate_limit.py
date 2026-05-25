"""限速 — 用 slowapi + Valkey storage。

Key function:跟 backend ``app/rate_limit.py`` 同行為 — 拿 JWT decoded
``sub``(username)當 key,沒 JWT 就 fallback IP。Valkey storage 讓多個 gateway
實例共享 quota(本次部署單實例,但 storage 已就位)。

Per-route limit:不在 slowapi 用 ``@limiter.limit("N/min")`` decorator(那種
是 route-handler-bound,改設定要重啟),改在 forward 之前手動 check
``cur_count > limit_n``。設定來源是 routes.yaml(``RoutesConfig.match``)。
"""
from __future__ import annotations

import logging
import re
import time
from typing import Optional

import redis.asyncio as redis_async
from fastapi import Request
from starlette.responses import JSONResponse

from .auth import extract_token, decode_token, AuthError
from .config import settings

_log = logging.getLogger("gateway.ratelimit")

# slowapi 的 "10/minute" 風格字串 → (count, seconds)
_RATE_RE = re.compile(r"^(\d+)\s*/\s*(second|minute|hour|day)$", re.IGNORECASE)
_PERIOD_SEC = {"second": 1, "minute": 60, "hour": 3600, "day": 86400}


def parse_rate(rate: str) -> Optional[tuple[int, int]]:
    """``"10/minute"`` → ``(10, 60)``;parse 失敗回 None。"""
    if not rate:
        return None
    m = _RATE_RE.match(rate.strip())
    if not m:
        return None
    return int(m.group(1)), _PERIOD_SEC[m.group(2).lower()]


class RateLimiter:
    """sliding-window 限速器,Valkey 計數。

    用 INCR + EXPIRE 模式:key = ``ratelimit:{period_sec}:{bucket_ts}:{ident}``,
    第一次 INCR 後立刻 EXPIRE(period_sec)。每個時間 bucket 一個 key,跨 bucket
    自動歸零。比 sliding-log 簡單,在 burst 邊界誤差最多 1x rate(可接受)。

    沒 Valkey 連線時 fallback in-memory dict(單實例 OK,擴 N 實例就失準)。
    """

    def __init__(self, redis_url: Optional[str]):
        self._redis_url = redis_url
        self._redis: Optional[redis_async.Redis] = None
        self._fallback: dict[str, tuple[int, float]] = {}   # key → (count, expire_ts)

    async def get_redis(self) -> Optional[redis_async.Redis]:
        if not self._redis_url:
            return None
        if self._redis is None:
            try:
                self._redis = redis_async.from_url(
                    self._redis_url, decode_responses=True,
                    socket_connect_timeout=2, socket_timeout=2,
                )
                await self._redis.ping()
            except Exception as e:
                _log.warning("redis connect failed: %s — falling back to in-memory", e)
                self._redis = None
                return None
        return self._redis

    async def check(self, ident: str, limit_count: int, period_sec: int) -> tuple[bool, int]:
        """回 ``(allowed, current_count)``。allowed=False 時 caller 該回 429。"""
        bucket_ts = int(time.time()) // period_sec
        key = f"ratelimit:{period_sec}:{bucket_ts}:{ident}"
        r = await self.get_redis()
        if r:
            try:
                cur = await r.incr(key)
                if cur == 1:
                    await r.expire(key, period_sec + 1)
                return (cur <= limit_count, cur)
            except Exception as e:
                _log.warning("redis ratelimit error: %s — fallback memory", e)
        # in-memory fallback
        now = time.time()
        c, exp = self._fallback.get(key, (0, now + period_sec))
        if exp < now:
            c, exp = 0, now + period_sec
        c += 1
        self._fallback[key] = (c, exp)
        # 順手清掉過期 key,避免吃記憶體
        if len(self._fallback) > 5000:
            self._fallback = {k: v for k, v in self._fallback.items() if v[1] >= now}
        return (c <= limit_count, c)


# 模組單例
limiter = RateLimiter(settings.redis_url)


def _identify_caller(request: Request) -> str:
    """拿 username(JWT decode 後)當 ratelimit key;沒 JWT 就用 X-Forwarded-For / client IP。"""
    token = extract_token(request)
    if token:
        try:
            payload = decode_token(token)
            sub = payload.get("sub") or payload.get("username")
            if sub:
                return f"user:{sub}"
        except AuthError:
            pass
    # IP fallback — 優先 X-Forwarded-For 第一個(nginx 已 set),沒有就 client.host
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return f"ip:{xff.split(',')[0].strip()}"
    return f"ip:{request.client.host if request.client else 'unknown'}"


async def enforce_rate_limit(request: Request, rate_str: str) -> Optional[JSONResponse]:
    """檢查限速;allowed → None,denied → 429 JSONResponse(caller 直回)。"""
    parsed = parse_rate(rate_str)
    if not parsed:
        return None   # 設定壞掉就不擋(故障開放,別把流量整個鎖死)
    limit_count, period_sec = parsed
    ident = _identify_caller(request)
    allowed, cur = await limiter.check(ident, limit_count, period_sec)
    if allowed:
        return None
    _log.info("rate limit hit: ident=%s rate=%s cur=%d", ident, rate_str, cur)
    return JSONResponse(
        {
            "detail": "Too Many Requests",
            "code": "rate_limited",
            "limit": rate_str,
            "current": cur,
        },
        status_code=429,
        headers={
            "Retry-After": str(period_sec),
            "X-RateLimit-Limit": str(limit_count),
            "X-RateLimit-Window-Seconds": str(period_sec),
        },
    )
