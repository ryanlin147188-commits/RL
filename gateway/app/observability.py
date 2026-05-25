"""structlog JSON + Prometheus 指標。

Lifespan startup 呼 init_logging() 把 stdlib logging 轉成 JSON。
Prometheus 指標由 prometheus-fastapi-instrumentator 自動收;這檔額外多 4 個
gateway-specific counter / histogram:gateway_upstream_latency_seconds、
gateway_rate_limit_rejections_total、gateway_auth_failures_total、
gateway_circuit_breaker_state(gauge,0=closed/1=half_open/2=open)。
"""
from __future__ import annotations

import logging
import sys
from typing import Optional

import structlog
from prometheus_client import Counter, Gauge, Histogram

from .config import settings


# ── Prometheus 指標 ───────────────────────────────────────────
upstream_latency = Histogram(
    "gateway_upstream_latency_seconds",
    "Backend response latency seconds (per method+path-pattern)",
    ["method", "path_pattern"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)
rate_limit_rejections = Counter(
    "gateway_rate_limit_rejections_total",
    "Requests rejected by rate limit",
    ["rate", "ident_kind"],
)
auth_failures = Counter(
    "gateway_auth_failures_total",
    "Auth failures (no token / invalid / expired)",
    ["reason"],
)
circuit_state_gauge = Gauge(
    "gateway_circuit_breaker_state",
    "Circuit breaker state (0=closed, 1=half_open, 2=open)",
    ["group"],
)


def init_logging() -> None:
    """把 stdlib logging 接到 structlog 的 JSON renderer。

    log_json=True → 給 ELK / Loki 解析的 JSON;False → 給人看的 console。
    """
    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    processors: list = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    if settings.log_json:
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer())

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
    # stdlib root logger → 統一 format(其他 lib 用 logging.getLogger() 也走 JSON)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
