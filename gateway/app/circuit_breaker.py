"""熔斷器 — 簡單計數 + reset timer 自己手刻。

不依賴 ``purgatory`` lib(API 變動快、文件少),50 行自己寫狀態機:

* state CLOSED:正常放行。失敗計數 ``> threshold`` → 切 OPEN
* state OPEN:直接拒絕(回給 caller 503),等 ``ttl_seconds`` 後切 HALF_OPEN
* state HALF_OPEN:放一個 request 試試;成功切回 CLOSED,失敗回 OPEN

跟 backend / DB 沒關係,純記憶體 — 多 gateway 實例各自獨立(可接受,因為熔斷
本來就是 best-effort 保護)。
"""
from __future__ import annotations

import logging
import time
from enum import Enum
from typing import Optional

from .routes_config import CircuitConfig

_log = logging.getLogger("gateway.circuit")


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    def __init__(self, name: str, threshold: int, ttl_seconds: int):
        self.name = name
        self.threshold = threshold
        self.ttl_seconds = ttl_seconds
        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._opened_at: Optional[float] = None

    @property
    def state(self) -> CircuitState:
        # 從 OPEN 自動過渡到 HALF_OPEN(time-based,不用背景 task)
        if self._state == CircuitState.OPEN and self._opened_at is not None:
            if time.time() - self._opened_at >= self.ttl_seconds:
                self._state = CircuitState.HALF_OPEN
                _log.info("circuit %s: OPEN → HALF_OPEN (ttl elapsed)", self.name)
        return self._state

    def allow_request(self) -> bool:
        """Caller 跑 upstream 之前先問:能不能跑?"""
        return self.state != CircuitState.OPEN

    def record_success(self) -> None:
        self._consecutive_failures = 0
        if self._state in (CircuitState.HALF_OPEN, CircuitState.OPEN):
            _log.info("circuit %s: %s → CLOSED (success)", self.name, self._state.value)
            self._state = CircuitState.CLOSED
            self._opened_at = None
            _set_metric(self.name, self._state)

    def record_failure(self) -> None:
        self._consecutive_failures += 1
        if self._state == CircuitState.HALF_OPEN:
            _log.warning("circuit %s: HALF_OPEN → OPEN (probe failed)", self.name)
            self._state = CircuitState.OPEN
            self._opened_at = time.time()
            _set_metric(self.name, self._state)
            return
        if self._consecutive_failures >= self.threshold:
            _log.warning(
                "circuit %s: CLOSED → OPEN (%d consecutive failures, threshold=%d)",
                self.name, self._consecutive_failures, self.threshold,
            )
            self._state = CircuitState.OPEN
            self._opened_at = time.time()
            _set_metric(self.name, self._state)


def _set_metric(group: str, state: CircuitState) -> None:
    """同步 Prometheus gauge:0=closed / 1=half_open / 2=open。

    Late import 避免 observability.py 跟 circuit_breaker.py 互相 import。
    Gauge label 一旦 set 就會持續匯出,不必每次都 set。
    """
    try:
        from .observability import circuit_state_gauge
        val = {CircuitState.CLOSED: 0, CircuitState.HALF_OPEN: 1, CircuitState.OPEN: 2}[state]
        circuit_state_gauge.labels(group=group).set(val)
    except Exception:
        pass

    def status_dict(self) -> dict:
        return {
            "name": self.name,
            "state": self.state.value,
            "consecutive_failures": self._consecutive_failures,
            "threshold": self.threshold,
            "ttl_seconds": self.ttl_seconds,
            "opened_at": self._opened_at,
        }


# ── Registry ─────────────────────────────────────────────────────
_breakers: dict[str, CircuitBreaker] = {}


def init_breakers(configs: dict[str, CircuitConfig]) -> None:
    """Lifespan startup 從 routes.yaml 載入,順手把 Prometheus gauge 設成 0(closed)。"""
    global _breakers
    _breakers = {
        name: CircuitBreaker(name, c.threshold, c.ttl_seconds)
        for name, c in configs.items()
    }
    if "default" not in _breakers:
        _breakers["default"] = CircuitBreaker("default", 10, 30)
    for name in _breakers:
        _set_metric(name, CircuitState.CLOSED)
    _log.info("circuit breakers initialized: %s", list(_breakers))


def get_breaker(group: str) -> CircuitBreaker:
    return _breakers.get(group) or _breakers.setdefault(
        group, CircuitBreaker(group, 10, 30),
    )


def all_status() -> list[dict]:
    return [b.status_dict() for b in _breakers.values()]
