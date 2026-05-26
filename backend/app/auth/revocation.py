"""JWT revocation list (Valkey-backed).

Per-token granularity: a successful ``POST /api/auth/logout`` writes the
caller's ``jti`` into the blocklist with a TTL equal to the token's
remaining lifetime. Subsequent requests bearing the same ``jti`` get
rejected at :class:`AuthMiddleware` before any handler runs.

Why not a database table?
- The blocklist is hot path, read on every authenticated request. Valkey
  ``EXISTS`` returns in microseconds; a Postgres roundtrip would dominate
  request latency.
- Rows expire automatically when the underlying token would have expired,
  so the store stays bounded without a cleanup job.

Failure mode (Valkey unreachable): :func:`is_revoked` returns ``False`` and
logs a warning. We deliberately fail-open rather than locking everyone out
when the cache is down -- token expiry already caps the blast radius at
``ACCESS_TOKEN_TTL_MINUTES``.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from app.config import settings

log = logging.getLogger(__name__)

_KEY_PREFIX = "jwt:revoked:"

# Lazily-built async Redis client; one per-process is plenty.
_async_redis = None


async def _get_async_redis():
    global _async_redis
    if _async_redis is None:
        # redis.asyncio is the official async client (same wire protocol as
        # Valkey). We import lazily so unit tests that never touch the
        # blocklist do not need a redis-py install at collection time.
        from redis import asyncio as aioredis

        _async_redis = aioredis.from_url(
            settings.REDIS_URL, encoding="utf-8", decode_responses=True
        )
    return _async_redis


async def revoke(jti: str, exp: Optional[int] = None) -> None:
    """Add ``jti`` to the blocklist until ``exp`` (Unix seconds).

    If ``exp`` is in the past or unset, default to a 24h TTL -- the only
    cost of an over-long entry is a few bytes; the only cost of too-short
    entries is letting a stolen token survive past logout.
    """
    if not jti:
        return
    ttl = max((exp or 0) - int(time.time()), 86_400)
    try:
        client = await _get_async_redis()
        await client.setex(f"{_KEY_PREFIX}{jti}", ttl, "1")
    except Exception as exc:  # noqa: BLE001
        # Don't let cache errors break logout; log and move on. The token
        # will still expire naturally.
        log.warning("token revocation cache write failed: %s", exc)


async def is_revoked(jti: Optional[str]) -> bool:
    """True if ``jti`` is in the blocklist.

    Fail-open: cache outages do NOT lock users out. The token's natural
    expiry remains the upper bound on a stolen-token's useful life.
    """
    if not jti:
        return False
    try:
        client = await _get_async_redis()
        return bool(await client.exists(f"{_KEY_PREFIX}{jti}"))
    except Exception as exc:  # noqa: BLE001
        log.warning("token revocation cache read failed (fail-open): %s", exc)
        return False
