"""Audit middleware：把每筆 mutating /api/* 請求寫入 audit_logs。

設計：
- 只攔 POST / PUT / PATCH / DELETE（GET 不寫，避免 log 爆量）
- /api/auth/* 與 /api/audit-logs 自己排除（防止登入登出寫一堆 + 自己的查詢無限遞迴）
- 用獨立 session 寫，避免污染 request 自己的 transaction（即使 request 失敗也要記）
- 失敗的請求（4xx/5xx）也記，方便看誰在亂打
"""
from __future__ import annotations

import logging
import re
import time
from typing import Optional

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware

log = logging.getLogger(__name__)

# 不審計的 path（regex）
_SKIP_PATTERNS = [
    re.compile(r"^/api/auth/(login|refresh|me)$"),
    re.compile(r"^/api/audit-logs(/|$)"),
]

# 從 path 推測 entity 類型；最後一段如果是 uuid 就當作 entity_id
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_PATH_ENTITY_RE = re.compile(r"^/api/([a-z][a-z0-9_-]*)")


def _path_to_entity(path: str) -> tuple[Optional[str], Optional[str]]:
    m = _PATH_ENTITY_RE.match(path)
    entity_type = m.group(1) if m else None
    # 找 path 中最後一段「像 uuid」的字串作為 entity_id
    parts = [p for p in path.split("/") if p]
    entity_id: Optional[str] = None
    for p in reversed(parts):
        if _UUID_RE.match(p):
            entity_id = p
            break
    # 找不到 uuid 但 path 末段不是 entity_type 本身 → 用最末段（給 by-username/{u} 這種）
    if not entity_id and len(parts) >= 2 and parts[-1] != entity_type:
        last = parts[-1]
        if last and last != entity_type and not last.startswith("?"):
            entity_id = last[:80]
    return entity_type, entity_id


def _should_skip(path: str, method: str) -> bool:
    if method.upper() not in {"POST", "PUT", "PATCH", "DELETE"}:
        return True
    if not path.startswith("/api/"):
        return True
    return any(p.search(path) for p in _SKIP_PATTERNS)


class AuditMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        method = request.method.upper()
        skip = _should_skip(path, method)

        started = time.perf_counter()
        response = await call_next(request)
        if skip:
            return response

        try:
            duration_ms = int((time.perf_counter() - started) * 1000)
            payload = getattr(request.state, "user_payload", None) or {}
            username = payload.get("sub") if isinstance(payload, dict) else None
            org_id = payload.get("org_id") if isinstance(payload, dict) else None
            entity_type, entity_id = _path_to_entity(path)
            ip = request.client.host if request.client else None
            ua = request.headers.get("user-agent")
            qs = request.url.query or None

            # 用獨立 session，不影響 request 的 commit/rollback 順序
            from app.database import AsyncSessionLocal
            from app.models.audit_log import AuditLog

            async with AsyncSessionLocal() as session:
                row = AuditLog(
                    organization_id=org_id,
                    username=username,
                    method=method,
                    path=path[:500],
                    entity_type=entity_type[:60] if entity_type else None,
                    entity_id=entity_id[:80] if entity_id else None,
                    status_code=response.status_code,
                    duration_ms=duration_ms,
                    ip_address=ip,
                    user_agent=(ua or "")[:500] or None,
                    request_query=(qs or "")[:2000] or None,
                )
                session.add(row)
                await session.commit()
        except Exception as e:
            # 審計失敗絕不能影響 request；只記 server log
            log.warning("audit middleware failed: %s", e)
        return response
