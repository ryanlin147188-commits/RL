"""OIDC login router(v1.1.5,authlib in-process)。

掛在 ``/api/auth/{provider}/{login,callback,enabled}``,被 middleware
whitelist 排除掉 JWT 驗證。流程:

1. SPA 按「使用 Zoho 登入」→ ``GET /api/auth/zoho/login``
   - 產 state、寫 HS256 cookie、302 redirect 到 IdP authorize URL
2. IdP 認證 → 302 帶 ``?code=&state=`` 回 ``GET /api/auth/zoho/callback``
   - 驗 state cookie
   - 用 code 跟 IdP 換 access_token → userinfo → 標準化 claims
   - JIT 建/找 ``users`` row(``oidc_provider`` + ``oidc_subject`` 為主鍵)
   - backend 自簽 HS256 access + refresh token 設 httpOnly cookies
   - 302 回 SPA ``/#oidc_login=1`` 讓前端 hydrate

設計取捨:

* 不用 authlib 的 ``OAuth`` registry / Starlette session,直接拿
  ``AsyncOAuth2Client`` + 自簽 state cookie 走 stateless flow,免引入
  SessionMiddleware 跟 backend 原本 fastapi.middleware 衝突
* state cookie ``typ=oidc_state``、TTL 10 分鐘、HttpOnly、SameSite=Lax;
  Lax 在 top-level navigation(從 Zoho 302 回來)是允許的,Strict 會在
  302 時被瀏覽器丟掉
* 失敗時 redirect 到 ``/?oidc_error=<code>&desc=<msg>``(不直接 raise 給
  backend 預設 500 頁,使用者看了會以為平台壞掉)
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

import jwt as pyjwt
from fastapi import APIRouter, Cookie, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import oidc as _oidc
from app.auth.fastapi_users_integration import (
    UserManager,
    get_jwt_strategy,
    get_user_manager,
)
from app.auth.security import (
    ACCESS_TOKEN_TTL_MINUTES,
    ACTIVE_ORG_COOKIE_NAME,
    ACTIVE_ORG_COOKIE_TTL_DAYS,
    JWT_ALGORITHM,
    JWT_SECRET,
    REFRESH_TOKEN_TTL_DAYS,
    create_refresh_token,
    should_use_secure_cookie as _should_use_secure_cookie,
    sign_active_org_cookie,
)
from app.database import get_db

logger = logging.getLogger(__name__)
router = APIRouter()

_STATE_COOKIE_NAME = "autotest_oidc_state"
_STATE_TTL_SECONDS = 600  # 10 分鐘


def _safe_redirect_path(raw: Optional[str]) -> str:
    """限制 redirect_to 必須是 same-origin 的 path,擋 open redirect。

    合法:``/``、``/projects``、``/projects/abc``、``/page?x=1``
    不合法:absolute URL(``http://``、``https://``、``//evil.com``)、
    含 CR/LF、scheme 偽裝(``javascript:``、``data:``)、反斜線繞過(``/\\evil``)

    任何不合法輸入一律退回 ``"/"``,不 raise — 對使用者來說登入仍然成功,
    只是落地頁回首頁。
    """
    if not raw or not isinstance(raw, str):
        return "/"
    target = raw.strip()
    # 控制字元 → 拒絕
    if any(c in target for c in ("\r", "\n", "\t", "\0")):
        return "/"
    # 必須以單斜線開頭(``/`` 但不是 ``//`` — 後者瀏覽器會視為 protocol-relative)
    if not target.startswith("/") or target.startswith("//"):
        return "/"
    # 反斜線繞過(IE/某些 proxy 把 ``\\`` 還原成 ``//``)
    if target.startswith("/\\"):
        return "/"
    return target


def _sign_state(state: str, provider: str, redirect_to: str) -> str:
    payload = {
        "state": state,
        "provider": provider,
        "redirect_to": redirect_to,
        "iat": datetime.utcnow(),
        "exp": datetime.utcnow() + timedelta(seconds=_STATE_TTL_SECONDS),
        "typ": "oidc_state",
    }
    return pyjwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def _verify_state(cookie: str) -> Optional[dict]:
    try:
        payload = pyjwt.decode(cookie, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except pyjwt.PyJWTError:
        return None
    if payload.get("typ") != "oidc_state":
        return None
    return payload


# ── /api/auth/{provider}/enabled — SPA 偵測按鈕用 ──────────────────────


@router.get("/auth/{provider}/enabled", tags=["U · 認證"])
async def oidc_enabled(provider: str) -> dict:
    """200 永遠回得到 ``{"enabled": bool}``,DevTools 不會留紅字。
    SPA ``_maybeShowZohoButton()`` 用它決定要不要亮按鈕。"""
    return {"enabled": _oidc.is_enabled(provider)}


# ── 舊 OIDC router compat stub ────────────────────────────────────────
# v1.1.3 之前有 ``/api/auth/oidc/providers``(回 OIDC provider 清單給 SPA 畫
# SSO 按鈕)。v1.1.5 router 完全卸載,但 SPA 登入頁仍會 probe 它 → 404 紅字。
# 回 200 空陣列保持安靜,authlib 版單 provider 用 env 配置不需要 DB-driven 清單。

@router.get("/auth/oidc/providers", tags=["U · 認證"])
async def oidc_providers_compat() -> list:
    return []


# ── /api/auth/{provider}/login — 302 到 IdP authorize ──────────────────


@router.get("/auth/{provider}/login", tags=["U · 認證"])
async def oidc_login(
    provider: str,
    request: Request,
    redirect_to: str = Query("/", description="登入完成後 SPA 跳到的路徑"),
):
    p = _oidc.get_provider(provider)
    if not p or not p.is_enabled():
        raise HTTPException(503, f"{provider} 登入未啟用(client_id / secret 未配)")

    # 擋 open redirect:redirect_to 必須是 same-origin path,不接受 absolute URL
    safe_redirect = _safe_redirect_path(redirect_to)

    state = _oidc.make_state()
    # v1.1.7 Phase 6:build_authorize_url 改成 async(httpx-oauth signature)
    authorize_url = await _oidc.build_authorize_url(p, state)

    resp = RedirectResponse(authorize_url, status_code=302)
    # Path 設成 "/" 而非 "/api/auth":部分瀏覽器(Chrome/Safari 對 path 沒結尾
    # 斜線時的 prefix matching)會在 Zoho 302 回 ``/api/auth/zoho/callback`` 時
    # 把 cookie 過濾掉,造成「缺少 state cookie」。state cookie 本身是 HS256
    # 自簽 JWT + 10 分鐘 TTL + HttpOnly + typ=oidc_state 三重驗,放寬 path
    # 不會多開攻擊面。
    resp.set_cookie(
        _STATE_COOKIE_NAME,
        _sign_state(state, provider, safe_redirect),
        max_age=_STATE_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        secure=_should_use_secure_cookie(request),
        path="/",
    )
    return resp


# ── /api/auth/{provider}/callback — token exchange + JIT + cookies ────


@router.get("/auth/{provider}/callback", tags=["U · 認證"])
async def oidc_callback(
    provider: str,
    request: Request,
    code: Optional[str] = Query(None),
    state: Optional[str] = Query(None),
    error: Optional[str] = Query(None),
    error_description: Optional[str] = Query(None),
    state_cookie: Optional[str] = Cookie(None, alias=_STATE_COOKIE_NAME),
    db: AsyncSession = Depends(get_db),
    user_manager: UserManager = Depends(get_user_manager),
):
    if error:
        return RedirectResponse(
            f"/?oidc_error={error}&desc={(error_description or '')[:200]}",
            status_code=302,
        )

    p = _oidc.get_provider(provider)
    if not p or not p.is_enabled():
        return RedirectResponse(
            f"/?oidc_error=provider_disabled&desc={provider}", status_code=302,
        )

    # callback 階段任何驗證失敗都 redirect 回 SPA 首頁帶 error,不要丟 JSON
    # detail page 給使用者看(會 confuse non-tech user)。SPA 偵測 hash 顯示
    # 友善訊息。
    def _err(code: str, desc: str) -> RedirectResponse:
        logger.warning("[oidc/%s/callback] %s: %s", provider, code, desc)
        return RedirectResponse(f"/?oidc_error={code}&desc={desc[:200]}", status_code=302)

    if not code or not state:
        return _err("missing_code_state", "callback 缺少 code 或 state")
    if not state_cookie:
        # v1.1.9 已改 path=/,若仍沒收到 → 通常是 cookie TTL(10 分鐘)過期、
        # 使用者開了多個 tab 互蓋 state、或瀏覽器擋 cookie。請重新登入。
        return _err(
            "missing_state_cookie",
            "缺少 state cookie;可能在 Zoho 登入頁停留超過 10 分鐘,或瀏覽器擋了 cookie。請重新點擊 Zoho 登入。",
        )
    decoded = _verify_state(state_cookie)
    if not decoded:
        return _err("invalid_state_cookie", "state cookie 無效或已過期,請重新登入")
    if decoded.get("state") != state:
        return _err("state_mismatch", "state 不符(可能多分頁互蓋),請重新登入")
    if decoded.get("provider") != provider:
        return _err("provider_mismatch", "state cookie 的 provider 跟 callback URL 不一致")

    try:
        token = await _oidc.exchange_code_for_token(p, code)
    except RuntimeError as e:
        logger.warning("%s token exchange failed: %s", provider, e)
        return RedirectResponse(
            f"/?oidc_error=token_exchange&desc={str(e)[:200]}", status_code=302,
        )

    try:
        raw_claims = await _oidc.fetch_userinfo(p, token["access_token"])
    except RuntimeError as e:
        logger.warning("%s userinfo failed: %s", provider, e)
        return RedirectResponse(
            f"/?oidc_error=userinfo&desc={str(e)[:200]}", status_code=302,
        )

    claims = _oidc.normalize_claims(p, raw_claims)
    if not claims.get("sub"):
        return RedirectResponse(
            "/?oidc_error=missing_sub&desc=IdP 沒回傳穩定的使用者識別",
            status_code=302,
        )

    # v1.1.8.1 Task 5:JIT 走 UserManager.get_or_provision_via_oidc。
    # password_helper / on_after_register hook / Casbin sync 都在 UserManager
    # 統一管理,routers/oidc_auth.py 不再自己 hash 密碼或塞 User row。
    try:
        user = await user_manager.get_or_provision_via_oidc(
            provider=provider,
            sub=claims["sub"],
            email=claims.get("email"),
            display_name=claims.get("display_name"),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("OIDC JIT provisioning failed: %s", exc)
        return RedirectResponse(
            f"/?oidc_error=provision&desc={str(exc)[:200]}", status_code=302,
        )
    await user_manager.on_after_login(user, request=request)
    await db.commit()

    # Access token 走 fastapi-users 的 JWTStrategy。refresh token 仍手刻
    # (fastapi-users 13 沒 refresh 概念)。
    access = await get_jwt_strategy().write_token(user)
    refresh = create_refresh_token(user.username)

    # 二次校驗 redirect_to(state cookie 本身雖然簽過,但若簽密鑰不慎洩漏,
    # 攻擊者可能偽造 cookie;這層 path-only 過濾是縱深防禦)
    target = _safe_redirect_path(decoded.get("redirect_to"))
    if "#" in target:
        target = target.split("#")[0]
    target = f"{target}#oidc_login=1"
    resp = RedirectResponse(target, status_code=302)
    is_https = _should_use_secure_cookie(request)
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


# v1.1.8.1 Task 5:之前在這裡的 ``_provision_from_claims`` 已搬到
# :meth:`UserManager.get_or_provision_via_oidc`,callback 直接用 dep 取
# user_manager 後呼這個方法。router 不再自己 hash 密碼或建 User row。
