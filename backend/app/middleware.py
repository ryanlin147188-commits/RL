"""Auth middleware：解析 Authorization: Bearer 並把 payload 注入 request.state。

策略：
- 對 /api/* 強制要求有效 access token；無效 / 過期 → 401
- whitelist：登入 / refresh / health / docs / openapi 不需要 token
- WebSocket / 靜態檔不在這條路徑下，不受影響
- 同時也接受 query param `?access_token=` 或 cookie `access_token` (給 SSE / 下載連結方便)

v1.1.10 (gateway short-circuit):
- 若 ``GATEWAY_BACKEND_SHARED_SECRET`` 環境變數有設且 request 帶合法
  ``X-Gateway-Verified`` HMAC,直接信任 gateway 已驗的 user / org / sub,
  跳過 JWT decode。HMAC 覆蓋 ``{method}\\n{path}\\n{sub}\\n{timestamp}``,
  timestamp 必須在 30 秒內(防 replay)。沒設 secret → 短路功能關閉,
  退回原本「gateway / backend 雙層各自驗 JWT」。
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import time
from typing import Iterable, Optional

import jwt as pyjwt
from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from app.auth.context import (
    reset_request_context,
    set_request_context,
)

_log = logging.getLogger(__name__)
# v1.1.8.1 Task 1:JWT decode 集中到 fastapi_users_integration,middleware 跟
# fastapi-users 的 read_token 共用同一份 JWT 規格定義。本檔不再自己呼
# ``security.decode_token`` — 改透過 ``decode_access_token_payload``,內部仍
# 是 PyJWT 解碼,只是出處變成 fastapi-users 整合層,避免兩處規格漂移。
from app.auth.fastapi_users_integration import decode_access_token_payload
from app.auth.revocation import is_revoked
from app.auth.security import (
    ACTIVE_ORG_COOKIE_NAME,
    verify_active_org_cookie,
)

# 不需登入的 path（regex match 整段 path）
_PUBLIC_PATTERNS: list[re.Pattern] = [
    re.compile(r"^/$"),
    re.compile(r"^/docs$"),
    re.compile(r"^/redoc$"),
    re.compile(r"^/openapi\.json$"),
    re.compile(r"^/api/auth/login$"),
    re.compile(r"^/api/auth/refresh$"),
    re.compile(r"^/api/auth/register$"),
    # v1.1.10 自助註冊重新啟用後加的 verify endpoints(全部匿名)
    re.compile(r"^/api/auth/register/verify-check$"),
    re.compile(r"^/api/auth/register/verify$"),
    re.compile(r"^/api/auth/register/resend-verify$"),
    re.compile(r"^/api/auth/bootstrap-invite$"),
    # Forgot-password 三步流程都需要匿名存取(從 email 連結點進來時還沒登入)
    re.compile(r"^/api/auth/forgot-password$"),
    re.compile(r"^/api/auth/reset-password$"),
    re.compile(r"^/api/auth/reset-password/check$"),
    # Self-service invite (Phase 4): anonymous user requests an invite by email
    re.compile(r"^/api/auth/request-access$"),
    re.compile(r"^/api/organizations/by-email-domain$"),
    # OIDC SSO：登入流程整段都不需要既有 token
    re.compile(r"^/api/auth/oidc/providers$"),
    re.compile(r"^/api/auth/oidc/login(/|$)"),
    re.compile(r"^/api/auth/oidc/callback$"),
    # v1.1.5 in-process OIDC(authlib + Zoho):login / callback / enabled probe
    # 都是匿名訪問,中間沒走 JWT 驗證,直到 callback 完成 backend 自簽 HS256 cookie。
    re.compile(r"^/api/auth/zoho/login$"),
    re.compile(r"^/api/auth/zoho/callback$"),
    re.compile(r"^/api/auth/zoho/enabled$"),
    # 舊 SPA 登入頁殘留的 probe;v1.1.5 後永遠回 200 [],只是讓 DevTools 不紅。
    re.compile(r"^/api/auth/oidc/providers$"),
    # Artifact routes perform their own scoped token / access-token validation.
    re.compile(r"^/pics/"),
    re.compile(r"^/results/"),
    # Recorder 容器(WEB / API 模式)在 codegen / mitmweb 結束時用 anonymous curl
    # 上傳 script_text / trace.zip / HAR;沒帶 Bearer token,容器靠 unguessable
    # session_id (UUID4 ~122 bit 熵) 當 capability。route handler 內仍會驗證
    # session 存在性,session_id 對不上一律 404。
    re.compile(r"^/api/recordings/[0-9a-fA-F-]+/upload$"),
    re.compile(r"^/api/recordings/[0-9a-fA-F-]+/upload-har$"),
    # OPTIONS 預檢一律放行(CORS)
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
        _promote_to_authorization_header(request, qp)
        return qp
    # cookie fallback（OIDC callback 把 token 塞 httpOnly cookie,前端 JS 讀不到也
    # 就拼不出 Authorization header;這裡把 cookie token promote 回 header,讓
    # fastapi-users 的 BearerTransport 在後續 Depends(get_current_user) 也能解開。
    # 不影響密碼登入流程,因為那條路徑 header 已經有值,不會走到這裡。）
    ck = request.cookies.get("access_token")
    if ck:
        _promote_to_authorization_header(request, ck)
        return ck
    return None


def _promote_to_authorization_header(request: Request, token: str) -> None:
    """把 cookie / query 取得的 token 補成 ``Authorization: Bearer <token>``,
    讓 fastapi-users 的 BearerTransport 在 Depends(get_current_user) 鏈中也能讀到。

    僅 mutate request.scope["headers"](ASGI 規範允許),
    不重建 Request 物件,starlette 後續讀 headers 時會看到新值。
    """
    headers = list(request.scope.get("headers", []))
    headers.append((b"authorization", f"Bearer {token}".encode("latin-1")))
    request.scope["headers"] = headers


def _resolve_active_org(payload: dict | None, request: Request) -> str | None:
    """決定本次 request 套用哪個 organisation。

    優先順序:

    1. ``active_org_id`` 簽章 cookie(Casdoor + 本地 dual-mode 都用這條)。
       簽章驗證會比對 cookie 的 ``sub`` 與 JWT ``sub`` 相符,避免他人偷 cookie。
    2. JWT payload 內的 ``org_id`` claim(本地 HS256 token 才會有;
       Casdoor RS256 token 沒這個 claim)。
    3. 都沒有 → None;ORM / scope 層當作「全域」處理(目前等於 superuser
       才能跨 org;一般 user 則會被 ensure_project_in_scope 擋掉)。
    """
    sub = (payload or {}).get("sub")
    cookie_val = request.cookies.get(ACTIVE_ORG_COOKIE_NAME)
    if cookie_val:
        org_from_cookie = verify_active_org_cookie(cookie_val, expected_sub=sub)
        if org_from_cookie:
            return org_from_cookie
    return (payload or {}).get("org_id")


def _payload_to_context(payload: dict | None, request: Request):
    """Push the JWT payload (if any) into the per-request ContextVars used by
    :mod:`app.auth.tenant` for query scoping and ORM auto-stamping.

    Returns the snapshot so the caller can ``reset_request_context`` after the
    response is produced — keeping the tenant scope strictly request-bound.
    """
    if not payload:
        return set_request_context(org_id=None, username=None, is_superuser=False)
    return set_request_context(
        org_id=_resolve_active_org(payload, request),
        username=payload.get("sub"),
        is_superuser=bool(payload.get("is_superuser", False)),
    )


# ── Gateway short-circuit(v1.1.10)─────────────────────────────
# 環境變數讀一次,避免 hot path 每個 request 都 os.environ.get
_GW_SHARED_SECRET: Optional[str] = os.environ.get("GATEWAY_BACKEND_SHARED_SECRET") or None
_GW_TIMESTAMP_TOLERANCE_SEC = 30


def _verify_gateway_hmac(request: Request) -> Optional[dict]:
    """檢查 ``X-Gateway-Verified`` 是否為 gateway 簽的合法 HMAC。

    合法 → 回 dict 重組好的 user_payload(跳過 JWT decode 直接拿 X-Gateway-*
    header 當權威);任一驗證失敗 → 回 None,由 caller 退回原 JWT 流程。
    """
    if not _GW_SHARED_SECRET:
        return None
    sig = request.headers.get("X-Gateway-Verified")
    ts_str = request.headers.get("X-Gateway-Timestamp")
    sub = request.headers.get("X-Gateway-Sub")
    if not (sig and ts_str and sub):
        return None
    try:
        ts = int(ts_str)
    except ValueError:
        return None
    if abs(int(time.time()) - ts) > _GW_TIMESTAMP_TOLERANCE_SEC:
        _log.warning("gateway HMAC timestamp out of tolerance (ts=%s)", ts)
        return None
    # 重算 HMAC(method 對 sign_gateway_request 一致用 upper case)
    method = request.method.upper()
    path = request.url.path
    msg = f"{method}\n{path}\n{sub}\n{ts}".encode("utf-8")
    expected = hmac.new(
        _GW_SHARED_SECRET.encode("utf-8"), msg, hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(sig, expected):
        _log.warning("gateway HMAC mismatch for %s %s sub=%s", method, path, sub)
        return None
    # 簽章 OK → 重組 payload(欄位對齊 JWT decode 後的 dict)
    payload: dict = {
        "sub": sub,
        "username": request.headers.get("X-Gateway-User") or sub,
        "is_superuser": request.headers.get("X-Gateway-Is-Superuser") == "1",
    }
    org = request.headers.get("X-Gateway-Org")
    if org:
        payload["org_id"] = org
    return payload


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
            payload: dict | None = None
            if token:
                try:
                    payload = decode_access_token_payload(token)
                except pyjwt.PyJWTError:
                    payload = None
            request.state.user_payload = payload
            snap = _payload_to_context(payload, request)
            try:
                return await call_next(request)
            finally:
                reset_request_context(snap)

        # ── Gateway short-circuit ──
        # Gateway 已驗 JWT → 跳過 decode + revocation,直接拿 X-Gateway-* 當權威
        gw_payload = _verify_gateway_hmac(request)
        if gw_payload is not None:
            # 即使 short-circuit,下游 ``Depends(get_current_user)``(fastapi-users
            # BearerTransport)仍從 Authorization header 取 token。SPA 在 OIDC 後
            # 只送 Cookie:access_token,本來 ``_extract_token`` 會 promote 到
            # Authorization header,short-circuit 跳過會讓 fastapi-users 拿不到。
            # 在這裡呼叫 ``_extract_token`` 純粹是為了觸發 promote(token 取出後
            # 不再用),確保下游 Depends 看得到 Authorization。
            _extract_token(request)
            request.state.user_payload = gw_payload
            snap = _payload_to_context(gw_payload, request)
            try:
                return await call_next(request)
            finally:
                reset_request_context(snap)

        # /api/* — 必須有有效 access token。decode_access_token_payload 內含
        # ``typ == access`` 檢查,refresh token 拿來打 /api/* 會直接被擋下。
        token = _extract_token(request)
        if not token:
            return JSONResponse(
                {"detail": "未授權：缺少 Authorization Bearer token"},
                status_code=401,
            )
        try:
            payload = decode_access_token_payload(token)
        except pyjwt.ExpiredSignatureError:
            return JSONResponse({"detail": "Token 已過期，請重新登入"}, status_code=401)
        except pyjwt.PyJWTError as e:
            return JSONResponse({"detail": f"Token 無效：{e}"}, status_code=401)

        # Token revocation check — rejects logged-out tokens before the handler
        # runs. Fail-open if the cache is unreachable (see revocation.is_revoked).
        if await is_revoked(payload.get("jti")):
            return JSONResponse({"detail": "Token 已撤銷,請重新登入"}, status_code=401)

        request.state.user_payload = payload
        snap = _payload_to_context(payload, request)
        try:
            return await call_next(request)
        finally:
            reset_request_context(snap)
