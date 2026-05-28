"""concurrency.try_acquire / release 單元測試。

不接真 Valkey。monkeypatch 把 _get_redis 換成 fake client(in-memory dict)。
fail-open 路徑用 raising client 模擬。
"""
from __future__ import annotations

import pytest

from app.agent import concurrency


class _FakeRedis:
    """In-memory fake — 模擬 INCR / EXPIRE / DECR / GET / DELETE。"""

    def __init__(self) -> None:
        self.store: dict[str, int] = {}
        self.ttls: dict[str, int] = {}

    async def incr(self, key):
        self.store[key] = self.store.get(key, 0) + 1
        return self.store[key]

    async def decr(self, key):
        self.store[key] = self.store.get(key, 0) - 1
        return self.store[key]

    async def expire(self, key, ttl):
        self.ttls[key] = ttl
        return True

    async def get(self, key):
        return str(self.store[key]) if key in self.store else None

    async def delete(self, key):
        self.store.pop(key, None)
        self.ttls.pop(key, None)
        return 1

    def pipeline(self, transaction=True):
        return _FakePipeline(self)


class _FakePipeline:
    def __init__(self, parent: _FakeRedis):
        self.parent = parent
        self.queued: list = []

    def incr(self, key):
        self.queued.append(("incr", key))

    def expire(self, key, ttl):
        self.queued.append(("expire", key, ttl))

    async def execute(self):
        results = []
        for op in self.queued:
            if op[0] == "incr":
                results.append(await self.parent.incr(op[1]))
            elif op[0] == "expire":
                results.append(await self.parent.expire(op[1], op[2]))
        self.queued.clear()
        return results

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None


class _BrokenRedis:
    """模擬 Valkey 連線斷掉。"""

    async def incr(self, *a, **k):
        raise ConnectionError("redis down")

    async def decr(self, *a, **k):
        raise ConnectionError("redis down")

    async def get(self, *a, **k):
        raise ConnectionError("redis down")

    async def delete(self, *a, **k):
        raise ConnectionError("redis down")

    def pipeline(self, transaction=True):
        raise ConnectionError("redis down")


@pytest.fixture(autouse=True)
def reset_singleton():
    """每個測試前後清掉 module-level singleton,避免污染。"""
    concurrency._async_redis = None
    yield
    concurrency._async_redis = None


@pytest.fixture
def fake_redis(monkeypatch):
    fake = _FakeRedis()

    async def _get():
        return fake

    monkeypatch.setattr(concurrency, "_get_redis", _get)
    return fake


@pytest.fixture
def broken_redis(monkeypatch):
    async def _get():
        return _BrokenRedis()

    monkeypatch.setattr(concurrency, "_get_redis", _get)


# ── try_acquire ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_try_acquire_first_call_succeeds(fake_redis) -> None:
    acquired, count = await concurrency.try_acquire(
        "user-A", "run_test_case", limit=3
    )
    assert acquired is True
    assert count == 1


@pytest.mark.asyncio
async def test_try_acquire_up_to_limit_succeeds(fake_redis) -> None:
    for i in range(3):
        acquired, count = await concurrency.try_acquire(
            "user-A", "run_test_case", limit=3
        )
        assert acquired is True
        assert count == i + 1


@pytest.mark.asyncio
async def test_try_acquire_over_limit_rejected_and_decrs(fake_redis) -> None:
    for _ in range(3):
        await concurrency.try_acquire("user-A", "run_test_case", limit=3)

    acquired, current = await concurrency.try_acquire(
        "user-A", "run_test_case", limit=3
    )
    assert acquired is False
    assert current == 3  # 被 DECR 撤回後仍是 3
    # 直接看 store 狀態應為 3(不是 4)
    assert fake_redis.store["agent:concur:user-A:run_test_case"] == 3


@pytest.mark.asyncio
async def test_try_acquire_per_user_per_tool_independent(fake_redis) -> None:
    """同 user 不同 tool 互不影響;不同 user 同 tool 也互不影響。"""
    await concurrency.try_acquire("user-A", "tool-X", limit=1)
    a, _ = await concurrency.try_acquire("user-A", "tool-X", limit=1)
    assert a is False  # tool-X 已滿

    a, _ = await concurrency.try_acquire("user-A", "tool-Y", limit=1)
    assert a is True  # tool-Y 還沒人用

    a, _ = await concurrency.try_acquire("user-B", "tool-X", limit=1)
    assert a is True  # user-B 是另一個 slot 池


@pytest.mark.asyncio
async def test_try_acquire_zero_limit_always_fails(fake_redis) -> None:
    acquired, count = await concurrency.try_acquire(
        "user-A", "tool", limit=0
    )
    assert acquired is False
    assert count == 0


@pytest.mark.asyncio
async def test_try_acquire_fail_open_when_redis_down(broken_redis) -> None:
    """Valkey 斷線 → 放行,但 log warning。"""
    acquired, count = await concurrency.try_acquire(
        "user-A", "run_test_case", limit=3
    )
    assert acquired is True
    assert count == 0  # fail-open 不確定 count


# ── release ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_release_decrements_count(fake_redis) -> None:
    await concurrency.try_acquire("user-A", "tool", limit=3)
    await concurrency.try_acquire("user-A", "tool", limit=3)
    assert fake_redis.store["agent:concur:user-A:tool"] == 2

    await concurrency.release("user-A", "tool")
    assert fake_redis.store["agent:concur:user-A:tool"] == 1


@pytest.mark.asyncio
async def test_release_at_zero_deletes_key(fake_redis) -> None:
    await concurrency.try_acquire("user-A", "tool", limit=3)
    await concurrency.release("user-A", "tool")
    # 歸零後 key 應被刪
    assert "agent:concur:user-A:tool" not in fake_redis.store


@pytest.mark.asyncio
async def test_release_fail_open_when_redis_down(broken_redis) -> None:
    # 不該 raise
    await concurrency.release("user-A", "tool")


# ── get_current_count ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_current_count_returns_zero_if_no_slot(fake_redis) -> None:
    assert await concurrency.get_current_count("user-A", "tool") == 0


@pytest.mark.asyncio
async def test_get_current_count_returns_n_after_acquires(fake_redis) -> None:
    await concurrency.try_acquire("user-A", "tool", limit=5)
    await concurrency.try_acquire("user-A", "tool", limit=5)
    assert await concurrency.get_current_count("user-A", "tool") == 2


@pytest.mark.asyncio
async def test_get_current_count_returns_none_when_redis_down(broken_redis) -> None:
    assert await concurrency.get_current_count("user-A", "tool") is None
