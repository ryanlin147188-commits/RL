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
from app.auth.security import (
    ACCESS_TOKEN_TTL_MINUTES,
    ACTIVE_ORG_COOKIE_NAME,
    ACTIVE_ORG_COOKIE_TTL_DAYS,
    JWT_ALGORITHM,
    JWT_SECRET,
    REFRESH_TOKEN_TTL_DAYS,
    create_access_token,
    create_refresh_token,
    sign_active_org_cookie,
)
from app.database import get_db

logger = logging.getLogger(__name__)
router = APIRouter()

_STATE_COOKIE_NAME = "autotest_oidc_state"
_STATE_TTL_SECONDS = 600  # 10 分鐘


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

    state = _oidc.make_state()
    # v1.1.7 Phase 6:build_authorize_url 改成 async(httpx-oauth signature)
    authorize_url = await _oidc.build_authorize_url(p, state)

    resp = RedirectResponse(authorize_url, status_code=302)
    resp.set_cookie(
        _STATE_COOKIE_NAME,
        _sign_state(state, provider, redirect_to),
        max_age=_STATE_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        path="/api/auth",
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

    if not code or not state:
        raise HTTPException(400, "callback 缺少 code / state")
    if not state_cookie:
        raise HTTPException(400, "缺少 state cookie;可能 cookie 過期或被瀏覽器擋下")
    decoded = _verify_state(state_cookie)
    if not decoded:
        raise HTTPException(400, "state cookie 無效或已過期")
    if decoded.get("state") != state:
        raise HTTPException(400, "state 不符(可能是 CSRF)")
    if decoded.get("provider") != provider:
        raise HTTPException(400, "state cookie 的 provider 跟 callback URL 不一致")

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

    # JIT provisioning(in-line,複用 security helpers)
    user = await _provision_from_claims(db, provider, claims)
    user.last_login_at = datetime.utcnow()
    await db.commit()

    # 自簽 HS256 token 給 SPA — 同 v1.1.2 密碼登入路徑,middleware 一視同仁
    extra = {"org_id": user.organization_id, "is_superuser": user.is_superuser}
    access = create_access_token(user.username, extra=extra)
    refresh = create_refresh_token(user.username)

    target = decoded.get("redirect_to") or "/"
    if "#" in target:
        target = target.split("#")[0]
    target = f"{target}#oidc_login=1"
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


# ── JIT provisioning helper ────────────────────────────────────────────


async def _provision_from_claims(db: AsyncSession, provider: str, claims: dict):
    """根據 normalized claims(``{sub, email, display_name}``)找/建 user。

    優先順序:
    1. ``(oidc_provider, oidc_subject)`` 對齊 → 已綁定的回鍋使用者
    2. ``email`` 對齊 → 之前是本地帳號 / 第一次走 SSO,直接綁上去
    3. 都沒有 → JIT 新建 user;role_id = NULL,沒 OrgMembership,
       使用者看得到 ``/api/auth/me`` 但所有專案級端點 deny(Casbin
       fail-closed)。管理員後續可在「設定 → 專案協作成員」加入專案。
    """
    import secrets as _secrets

    from sqlalchemy import select

    from app.auth.security import hash_password
    from app.models.user import User

    sub = claims["sub"]
    email = claims.get("email")
    display = claims.get("display_name")

    # 1) by (provider, sub)
    user = (
        await db.execute(
            select(User)
            .where(User.oidc_provider == provider)
            .where(User.oidc_subject == sub)
        )
    ).scalar_one_or_none()

    # 2) by email(沒綁過任何 provider)
    if user is None and email:
        user = (
            await db.execute(select(User).where(User.email == email))
        ).scalar_one_or_none()
        if user is not None:
            user.oidc_provider = provider
            user.oidc_subject = sub

    if user is not None:
        if not user.is_active:
            raise HTTPException(403, f"使用者「{user.username}」已被停用")
        # 更新 display_name 為最新值(讓 IdP 端改名能反映過來)
        if display and user.display_name != display:
            user.display_name = display
        return user

    # 3) JIT 新建
    username = (email or sub).lower()
    # 跟 SSO-only 走的路徑一樣:password_hash 用隨機 32 byte;管理員之後
    # 想讓他用密碼登入再走 ``/auth/users/{username}/reset-password``。
    user = User(
        username=username,
        display_name=display or username,
        email=email,
        password_hash=hash_password(_secrets.token_urlsafe(32)),
        oidc_provider=provider,
        oidc_subject=sub,
        is_active=True,
        is_superuser=False,
    )
    db.add(user)
    await db.flush()
    return user
