"""Auth middleware：解析 Authorization: Bearer 並把 payload 注入 request.state。

策略：
- 對 /api/* 強制要求有效 access token；無效 / 過期 → 401
- whitelist：登入 / refresh / health / docs / openapi 不需要 token
- WebSocket / 靜態檔不在這條路徑下，不受影響
- 同時也接受 query param `?access_token=` 或 cookie `access_token` (給 SSE / 下載連結方便)
"""
from __future__ import annotations

import re
from typing import Iterable

import jwt as pyjwt
from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from app.auth.security import decode_token

# 不需登入的 path（regex match 整段 path）
_PUBLIC_PATTERNS: list[re.Pattern] = [
    re.compile(r"^/$"),
    re.compile(r"^/docs$"),
    re.compile(r"^/redoc$"),
    re.compile(r"^/openapi\.json$"),
    re.compile(r"^/api/auth/login$"),
    re.compile(r"^/api/auth/refresh$"),
    # OIDC SSO：登入流程整段都不需要既有 token
    re.compile(r"^/api/auth/oidc/providers$"),
    re.compile(r"^/api/auth/oidc/login(/|$)"),
    re.compile(r"^/api/auth/oidc/callback$"),
    # 靜態檔（截圖 / 結果）— 仍以反向代理保護，這裡放行讓 nginx 直接服務
    re.compile(r"^/pics/"),
    re.compile(r"^/results/"),
    # OPTIONS 預檢一律放行（CORS）
]


def _is_public(path: str) -> bool:
    return any(p.match(path) for p in _PUBLIC_PATTERNS)


def _extract_token(request: Request) -> str | None:
    auth = request.headers.get("authorization") or request.headers.get("Authorization")
    if auth and auth.lower().startswith("bearer "):
        return auth.split(" ", 1)[1].strip()
    # query param fallback（給檔案下載連結用）
    qp = request.query_params.get("access_token")
    if qp:
        return qp
    # cookie fallback
    ck = request.cookies.get("access_token")
    if ck:
        return ck
    return None


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        method = request.method.upper()
        path = request.url.path

        # CORS 預檢一律放行
        if method == "OPTIONS":
            return await call_next(request)

        # 非 /api/* 與 whitelist 直接放行（並仍嘗試解 token，方便靜態頁面取使用者資訊）
        if not path.startswith("/api/") or _is_public(path):
            token = _extract_token(request)
            if token:
                try:
                    request.state.user_payload = decode_token(token)
                except pyjwt.PyJWTError:
                    request.state.user_payload = None
            else:
                request.state.user_payload = None
            return await call_next(request)

        # /api/* — 必須有有效 access token
        token = _extract_token(request)
        if not token:
            return JSONResponse(
                {"detail": "未授權：缺少 Authorization Bearer token"},
                status_code=401,
            )
        try:
            payload = decode_token(token)
        except pyjwt.ExpiredSignatureError:
            return JSONResponse({"detail": "Token 已過期，請重新登入"}, status_code=401)
        except pyjwt.PyJWTError as e:
            return JSONResponse({"detail": f"Token 無效：{e}"}, status_code=401)

        if payload.get("typ") != "access":
            return JSONResponse(
                {"detail": "需要 access token（不是 refresh token）"}, status_code=401
            )

        request.state.user_payload = payload
        return await call_next(request)
