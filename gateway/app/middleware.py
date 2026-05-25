"""自訂 ASGI middleware:RequestId、Access log。

Commit 1 只放最基本的 X-Request-Id 跟 access log;Commit 4 才會接 structlog
跟 prometheus。先用 stdlib logging 撐著,後面再升級不影響介面。
"""
from __future__ import annotations

import logging
import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

_log = logging.getLogger("gateway.access")


class RequestIdMiddleware(BaseHTTPMiddleware):
    """每個 request 注一個 X-Request-Id 進 request.state。

    如果 client 已經帶了 X-Request-Id(例如從前端 SPA 帶來)就用它的;沒帶就
    用 uuid7-style(目前 stdlib 沒 uuid7,先用 uuid4 hex 替代 — 排序友好性
    可接受)。值會在 :mod:`http_proxy` 被讀出來注進上游 header,再進到 backend
    AuditMiddleware 寫進 audit_log,完成跨服務串接。
    """

    async def dispatch(self, request: Request, call_next):
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex
        request.state.request_id = rid
        response = await call_next(request)
        response.headers["x-request-id"] = rid
        return response


class AccessLogMiddleware(BaseHTTPMiddleware):
    """印 method / path / status / 延遲 / request_id 的 access log。"""

    async def dispatch(self, request: Request, call_next):
        start = time.perf_counter()
        try:
            response = await call_next(request)
            status = response.status_code
        except Exception:
            elapsed_ms = (time.perf_counter() - start) * 1000
            _log.exception(
                "gateway %s %s 500 %.1fms rid=%s",
                request.method,
                request.url.path,
                elapsed_ms,
                getattr(request.state, "request_id", "-"),
            )
            raise
        elapsed_ms = (time.perf_counter() - start) * 1000
        _log.info(
            "gateway %s %s %d %.1fms rid=%s",
            request.method,
            request.url.path,
            status,
            elapsed_ms,
            getattr(request.state, "request_id", "-"),
        )
        return response
