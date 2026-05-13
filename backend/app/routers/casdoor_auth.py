"""Casdoor OAuth2 / OIDC 登入 endpoints。

掛在 ``/api/auth/casdoor/login`` + ``/api/auth/callback``,被 middleware
whitelist 排除掉 auth 檢查。流程:

1. SPA 按「Sign in with Casdoor」→ 打 ``GET /api/auth/casdoor/login``
   後端:產 state、寫 cookie、302 redirect 到 Casdoor 的 authorize endpoint。
2. Casdoor 認證完使用者 → 302 帶 ``?code=&state=`` 回 ``/api/auth/callback``。
   後端:
   * 驗 state cookie
   * 用 code 跟 Casdoor 換 token
   * JWKS 驗 id_token(沒 id_token 退回 ``/api/userinfo``)
   * JIT 建/找 User row
   * 設 httpOnly cookies(``access_token`` / ``refresh_token`` / ``active_org_id``)
   * 302 回 ``/`` 讓 SPA 自己 hydrate

Phase 4 之後,當 Casdoor 完全取代密碼登入,``/api/auth/login`` 會下架,SPA
的「登入」按鈕直接打這支 ``/login``;在此之前兩條路徑並存。
"""
from __future__ import annotations

import logging
import secrets
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Cookie, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import casdoor as _casdoor
from app.auth.security import (
    ACCESS_TOKEN_TTL_MINUTES,
    ACTIVE_ORG_COOKIE_NAME,
    ACTIVE_ORG_COOKIE_TTL_DAYS,
    REFRESH_TOKEN_TTL_DAYS,
    create_access_token,
    create_refresh_token,
    sign_active_org_cookie,
)
from app.database import get_db

logger = logging.getLogger(__name__)
router = APIRouter()

# state cookie 走 plain HMAC-signed JWT(共用 AUTOTEST_JWT_SECRET),不另外
# 抓 Fernet — Casdoor flow 不像舊 oidc 還需要 nonce + provider_id 包進
# Fernet,單一 state 用 HS256 JWT 就夠。
_STATE_COOKIE_NAME = "autotest_casdoor_state"
_STATE_TTL_SECONDS = 600  # 10 分鐘


def _state_cookie_payload(state: str, redirect_to: str) -> str:
    from datetime import timedelta

    import jwt as pyjwt

    from app.auth.security import JWT_ALGORITHM, JWT_SECRET

    payload = {
        "state": state,
        "redirect_to": redirect_to,
        "iat": datetime.utcnow(),
        "exp": datetime.utcnow() + timedelta(seconds=_STATE_TTL_SECONDS),
        "typ": "casdoor_state",
    }
    return pyjwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def _decode_state_cookie(cookie: str) -> Optional[dict]:
    import jwt as pyjwt

    from app.auth.security import JWT_ALGORITHM, JWT_SECRET

    try:
        payload = pyjwt.decode(cookie, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except pyjwt.PyJWTError:
        return None
    if payload.get("typ") != "casdoor_state":
        return None
    return payload


def _redirect_uri(request: Request) -> str:
    """組出 callback URL。Casdoor 設定的 redirect_uris 必須完全相符,所以
    優先用環境變數固定的 CASDOOR_REDIRECT_URL,沒設定才從 request.base_url 推。
    """
    if _casdoor.CASDOOR_REDIRECT_URL:
        return _casdoor.CASDOOR_REDIRECT_URL
    base = str(request.base_url).rstrip("/")
    return f"{base}/api/auth/callback"


@router.get("/auth/casdoor/enabled", tags=["U · 認證"])
async def casdoor_enabled() -> dict:
    """SPA 偵測 Casdoor 是否啟用的輕量 probe。200 永遠回得到,內容:

    * ``enabled``:bool,是否啟用 Casdoor SSO
    * ``admin_base``:對外 Casdoor URL(``CASDOOR_PUBLIC_ENDPOINT``),SPA 用
      它組「建立新使用者 / 編輯角色」之類連到 Casdoor admin UI 的連結。沒
      啟用時回空字串。

    為什麼不直接 probe ``/auth/casdoor/login``:那條未啟用時是 503,瀏覽器
    DevTools 一定會印紅字。改用此 200 端點後 SPA 拿到布林值再決定要不要亮
    按鈕,DevTools 不會留下假象錯誤。
    """
    enabled = _casdoor.is_enabled()
    return {
        "enabled": enabled,
        "admin_base": _casdoor.CASDOOR_PUBLIC_ENDPOINT if enabled else "",
    }


@router.get("/auth/oidc/providers", tags=["U · 認證"])
async def oidc_providers_compat() -> list:
    """OIDC providers 在 Phase 5 cutover 後由 Casdoor 接管,本端 ``oidc_providers``
    表已 drop。SPA 登入頁仍會 fetch 這條來決定要不要畫 SSO 按鈕清單;改回
    空陣列 200(而不是 404),避免 DevTools 留下假象錯誤。

    需要設定 SSO 時請進 Casdoor admin UI → Application → Providers。
    """
    return []


@router.get("/auth/casdoor/login", tags=["U · 認證"])
async def casdoor_login(
    request: Request,
    redirect_to: str = Query("/", description="登入完成後 SPA 跳到的路徑"),
    provider: Optional[str] = Query(
        None,
        description=(
            "Casdoor provider 名稱(例:zoho-corp);帶入後 Casdoor 會略過自家"
            "登入頁直接 302 到該 upstream IdP。空值 → 顯示 Casdoor 預設登入頁。"
        ),
    ),
):
    """302 redirect 到 Casdoor 的 OAuth2 authorize endpoint。

    SPA 端只要 ``window.location = '/api/auth/casdoor/login'`` 即可,不需要
    自己組 client_id / state。需要直接走 Zoho / Google 等第三方時帶
    ``?provider=<casdoor-provider-name>``。
    """
    if not _casdoor.is_enabled():
        raise HTTPException(503, "Casdoor 登入未啟用(CASDOOR_ENABLED=False)")
    state = _casdoor.make_state()
    redirect_uri = _redirect_uri(request)
    authorize_url = _casdoor.build_authorize_url(redirect_uri, state, provider=provider)
    resp = RedirectResponse(authorize_url, status_code=302)
    resp.set_cookie(
        _STATE_COOKIE_NAME,
        _state_cookie_payload(state, redirect_to),
        max_age=_STATE_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        path="/api/auth",
    )
    return resp


@router.get("/auth/callback", tags=["U · 認證"])
async def casdoor_callback(
    request: Request,
    code: Optional[str] = Query(None),
    state: Optional[str] = Query(None),
    error: Optional[str] = Query(None),
    error_description: Optional[str] = Query(None),
    state_cookie: Optional[str] = Cookie(None, alias=_STATE_COOKIE_NAME),
    db: AsyncSession = Depends(get_db),
):
    """Casdoor OAuth2 callback。

    成功 → set httpOnly cookies + 302 回 SPA;失敗 → 302 回 ``/?casdoor_error=...``
    讓 SPA 顯示錯誤訊息(不直接 raise,避免使用者看到 backend stacktrace 頁面)。
    """
    if not _casdoor.is_enabled():
        raise HTTPException(503, "Casdoor 登入未啟用(CASDOOR_ENABLED=False)")

    if error:
        return RedirectResponse(
            f"/?casdoor_error={error}&desc={(error_description or '')[:200]}",
            status_code=302,
        )
    if not code or not state:
        raise HTTPException(400, "callback 缺少 code / state")
    if not state_cookie:
        raise HTTPException(400, "缺少 state cookie;可能 cookie 過期或被瀏覽器擋下")
    decoded = _decode_state_cookie(state_cookie)
    if not decoded:
        raise HTTPException(400, "state cookie 無效或已過期")
    if decoded.get("state") != state:
        raise HTTPException(400, "state 不符(可能是 CSRF)")

    redirect_uri = _redirect_uri(request)
    try:
        token_resp = await _casdoor.exchange_code_for_token(code, redirect_uri)
    except RuntimeError as e:
        logger.warning("Casdoor token exchange failed: %s", e)
        return RedirectResponse(
            f"/?casdoor_error=token_exchange&desc={str(e)[:200]}", status_code=302
        )

    id_token = token_resp.get("id_token") or token_resp.get("access_token")
    if not id_token:
        return RedirectResponse(
            "/?casdoor_error=no_token&desc=Casdoor 沒回傳 id_token 也沒回傳 access_token",
            status_code=302,
        )
    try:
        claims = _casdoor.decode_casdoor_jwt(id_token)
    except Exception as e:  # pyjwt.PyJWTError + 一切 lazy import 失敗
        logger.warning("Casdoor JWT verify failed: %s", e)
        return RedirectResponse(
            f"/?casdoor_error=jwt_verify&desc={str(e)[:200]}", status_code=302
        )

    try:
        user = await _casdoor.provision_user_from_casdoor_claims(db, claims)
    except RuntimeError as e:
        logger.warning("Casdoor provisioning failed: %s", e)
        return RedirectResponse(
            f"/?casdoor_error=provision&desc={str(e)[:200]}", status_code=302
        )

    user.last_login_at = datetime.utcnow()
    await db.flush()

    # 本地簽 access + refresh 給 SPA 用 — Casdoor 自己也有發但我們不直接用:
    # 1. 我們的 access_token 有 jti + Valkey blocklist,Casdoor 沒接這套
    # 2. middleware 仍會走 dual-mode,如果想直接吃 Casdoor token 也支援
    # 兩條路徑並存,讓 Phase 4 之前任一 broken 都不會把使用者鎖在外面
    extra = {"org_id": user.organization_id, "is_superuser": user.is_superuser}
    access = create_access_token(user.username, extra=extra)
    refresh = create_refresh_token(user.username)

    # 302 回 SPA;cookie 域跟 path 都設根目錄,讓 fetch wrapper 自動帶上。
    # 後綴 ``#casdoor_login=1`` 給 SPA pre-paint script + post-load hydrate
    # 用 — JS 讀不到 httpOnly cookie,需要這個 hash 才知道「我是剛從 OIDC
    # callback 回來的」,跑一次 ``/api/auth/me`` 把使用者資訊抓回去 localStorage
    # display state,並把登入 overlay 收起來。
    target = decoded.get("redirect_to") or "/"
    if "#" in target:
        target = target.split("#")[0]
    target = f"{target}#casdoor_login=1"
    resp = RedirectResponse(target, status_code=302)
    is_https = request.url.scheme == "https"
    resp.set_cookie(
        "access_token", access,
        max_age=ACCESS_TOKEN_TTL_MINUTES * 60,
        httponly=True, samesite="lax", secure=is_https, path="/",
    )
    resp.set_cookie(
        "refresh_token", refresh,
        max_age=REFRESH_TOKEN_TTL_DAYS * 24 * 3600,
        httponly=True, samesite="lax", secure=is_https, path="/api/auth/refresh",
    )
    if user.organization_id:
        resp.set_cookie(
            ACTIVE_ORG_COOKIE_NAME,
            sign_active_org_cookie(user.username, user.organization_id),
            max_age=ACTIVE_ORG_COOKIE_TTL_DAYS * 24 * 3600,
            httponly=True, samesite="lax", secure=is_https, path="/",
        )
    resp.delete_cookie(_STATE_COOKIE_NAME, path="/api/auth")
    return resp
