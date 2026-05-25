"""Gateway 端 JWT 驗證 + Backend HMAC 簽章。

* :func:`verify_jwt` — 從 Authorization / query / cookie 取出 JWT,decode 後回
  payload。失敗 raise ``AuthError``。
* :func:`is_public_path` — 路徑是否在白名單(不需 token)。完整複製 backend
  ``_PUBLIC_PATTERNS``,加上 OIDC 三條 Zoho callback。
* :func:`sign_gateway_request` — 用 ``GATEWAY_BACKEND_SHARED_SECRET`` 對請求
  metadata 做 HMAC-SHA256,backend 端短路驗證用。

Commit 4 會在這個檔案再加 :func:`verify_api_key`,把 ``X-API-Key`` 認證接進來。
"""
from __future__ import annotations

import hashlib
import hmac
import re
import time
from typing import Any, Optional

import jwt as pyjwt
from fastapi import Request

from .config import settings


class AuthError(Exception):
    """JWT 解碼失敗 / token 過期 / 撤銷等驗證錯誤;對應 401。"""

    def __init__(self, detail: str, code: str = "unauthorized"):
        self.detail = detail
        self.code = code
        super().__init__(detail)


# ── Public path patterns ────────────────────────────────────────────
# 完整複製 backend/app/middleware.py:_PUBLIC_PATTERNS,加幾條 OIDC。
# 命中任一條 → gateway 不驗 token 直接 forward,backend 自己負責。
_PUBLIC_PATTERNS: list[re.Pattern] = [
    re.compile(r"^/$"),
    re.compile(r"^/healthz$"),
    re.compile(r"^/readyz$"),
    re.compile(r"^/metrics$"),
    re.compile(r"^/api/healthz$"),
    re.compile(r"^/api/auth/login$"),
    re.compile(r"^/api/auth/refresh$"),
    re.compile(r"^/api/auth/register$"),
    # v1.1.10 自助註冊 verify endpoints — 跟 backend middleware.py 對齊
    re.compile(r"^/api/auth/register/verify-check$"),
    re.compile(r"^/api/auth/register/verify$"),
    re.compile(r"^/api/auth/register/resend-verify$"),
    re.compile(r"^/api/auth/bootstrap-invite$"),
    re.compile(r"^/api/auth/forgot-password$"),
    re.compile(r"^/api/auth/reset-password$"),
    re.compile(r"^/api/auth/reset-password/check$"),
    re.compile(r"^/api/auth/request-access$"),
    re.compile(r"^/api/organizations/by-email-domain$"),
    re.compile(r"^/api/auth/oidc/providers$"),
    re.compile(r"^/api/auth/oidc/login(/|$)"),
    re.compile(r"^/api/auth/oidc/callback$"),
    re.compile(r"^/api/auth/zoho/login$"),
    re.compile(r"^/api/auth/zoho/callback$"),
    re.compile(r"^/api/auth/zoho/enabled$"),
    # Artifact streaming 用自己的 scoped token
    re.compile(r"^/pics/"),
    re.compile(r"^/results/"),
    # Recorder 容器 anonymous upload(capability token UUID4)
    re.compile(r"^/api/recordings/[0-9a-fA-F-]+/upload$"),
    re.compile(r"^/api/recordings/[0-9a-fA-F-]+/upload-har$"),
]


def is_public_path(path: str) -> bool:
    return any(p.match(path) for p in _PUBLIC_PATTERNS)


# ── Token 取出 ─────────────────────────────────────────────────────
def extract_token(request: Request) -> Optional[str]:
    """跟 backend ``_extract_token`` 同優先序:Header → query → cookie。"""
    auth = request.headers.get("authorization") or request.headers.get("Authorization")
    if auth and auth.lower().startswith("bearer "):
        return auth.split(" ", 1)[1].strip()
    qp = request.query_params.get("access_token")
    if qp:
        return qp
    ck = request.cookies.get("access_token")
    if ck:
        return ck
    return None


def decode_token(token: str) -> dict[str, Any]:
    """Decode HS256 JWT,只檢查 typ=access。

    失敗 raise AuthError(401)。Revocation check 不在 gateway 做(需要 Valkey
    跟 backend 共享,留給 backend AuthMiddleware 第二層驗;gateway 只擋簽章 /
    過期 / typ 不符)。
    """
    try:
        payload = pyjwt.decode(
            token,
            settings.autotest_jwt_secret,
            algorithms=[settings.jwt_algorithm],
            options={"require": ["exp"]},
        )
    except pyjwt.ExpiredSignatureError:
        raise AuthError("Token 已過期,請重新登入", code="token_expired")
    except pyjwt.PyJWTError as e:
        raise AuthError(f"Token 無效:{e}", code="token_invalid")

    typ = payload.get("typ", "access")
    if typ != "access":
        raise AuthError(
            f"Token type {typ!r} 不允許(需 access);refresh token 不能用於 API 呼叫",
            code="token_wrong_type",
        )
    return payload


def verify_jwt(request: Request) -> dict[str, Any]:
    """從 request 取 token 並 decode,回 payload。沒 token raise AuthError。"""
    token = extract_token(request)
    if not token:
        raise AuthError("未授權:缺少 Authorization Bearer token", code="no_token")
    return decode_token(token)


# ── Gateway → Backend HMAC ────────────────────────────────────────
def sign_gateway_request(
    method: str,
    path: str,
    user_sub: str,
    timestamp: Optional[int] = None,
) -> tuple[str, int]:
    """為 ``X-Gateway-Verified`` header 計算 HMAC-SHA256 簽章。

    回傳 ``(signature_hex, timestamp)``;backend 同 secret 重算就能驗。
    對齊欄位:``{method}\\n{path}\\n{user_sub}\\n{timestamp}``。Backend 收到後
    驗 ``abs(now - timestamp) <= 30s`` 防 replay。
    """
    secret = settings.gateway_backend_shared_secret
    if not secret:
        return ("", 0)
    ts = timestamp if timestamp is not None else int(time.time())
    msg = f"{method.upper()}\n{path}\n{user_sub}\n{ts}".encode("utf-8")
    sig = hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()
    return (sig, ts)


def has_shared_secret() -> bool:
    """Backend short-circuit 用得到嗎?沒 secret 就走純雙層獨立驗證。"""
    return bool(settings.gateway_backend_shared_secret)


# ── API Key 驗證(v1.1.10 Commit 4)─────────────────────────────
# Gateway 收到 ``X-API-Key: ak_xxxx``:
# 1) 先 SHA256 hash 跟 backend ``/api/auth/api-keys/verify`` 拿 user metadata
# 2) 在 gateway 本地 mint 5 分鐘 JWT(同樣 AUTOTEST_JWT_SECRET 簽,backend 端
#    AuthMiddleware 把它當一般 JWT 解,完全相容)
# 3) Replace request 的 Authorization header,後續流程跟 JWT request 一樣
async def verify_api_key(
    plain_key: str, backend_client,
) -> Optional[dict]:
    """打 backend /api/auth/api-keys/verify 查 key。

    backend_client 是 httpx.AsyncClient(共用 http_proxy 那支)。回 dict 含
    user_id / username / organization_id / is_superuser / scopes,或 None。

    Auth header 給 backend 看的:用 GATEWAY_BACKEND_SHARED_SECRET HMAC 簽
    一個 method=POST path=/api/auth/api-keys/verify sub=gateway 的 header,
    backend AuthMiddleware short-circuit 把 sub=gateway 重組進 user_payload,
    /verify endpoint 內看到 sub=='gateway' 才放行(免一般 user 拿 key 來查
    其他 user 的 key)。
    """
    import time as _t
    if not plain_key.startswith("ak_"):
        return None
    method = "POST"
    path = "/api/auth/api-keys/verify"
    ts = int(_t.time())
    sig, _ = sign_gateway_request(method, path, "gateway", timestamp=ts)
    if not sig:
        # 沒 shared secret,API key 功能無法使用(backend 不會信我們)
        return None
    headers = {
        "X-Gateway-Verified": sig,
        "X-Gateway-Timestamp": str(ts),
        "X-Gateway-Sub": "gateway",
        "X-Gateway-User": "gateway",
        "Content-Type": "application/json",
    }
    try:
        r = await backend_client.post(path, json={"api_key": plain_key}, headers=headers)
    except Exception:
        return None
    if r.status_code != 200:
        return None
    data = r.json()
    return {
        "sub": data["username"],         # 跟 backend JWT 同欄位
        "username": data["username"],
        "user_id": data["user_id"],
        "org_id": data.get("organization_id"),
        "is_superuser": bool(data.get("is_superuser")),
        "scopes": data.get("scopes"),
        # 標記:這條 request 是 API key 來的,給 audit / metrics 用
        "_api_key": True,
    }


def mint_short_jwt(payload: dict, ttl_seconds: int = 300) -> str:
    """從 API key payload 產一個短命 JWT 給 backend(backend 也可拒收 _api_key)。"""
    import time as _t
    now = int(_t.time())
    claims = {
        "sub": payload["sub"],
        "username": payload["username"],
        "iat": now,
        "exp": now + ttl_seconds,
        "typ": "access",
        "org_id": payload.get("org_id"),
        "is_superuser": payload.get("is_superuser", False),
        # jti 給 backend revocation 用(API key 衍生的 JWT 不會被 revoke,
        # 但 backend revoke cache 預期欄位存在,給個 deterministic value)
        "jti": f"apikey-{payload.get('user_id', '?')}-{now}",
    }
    return pyjwt.encode(
        claims, settings.autotest_jwt_secret, algorithm=settings.jwt_algorithm,
    )
