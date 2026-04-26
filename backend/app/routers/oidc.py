"""OIDC SSO REST endpoints。

路徑：
  GET  /api/auth/oidc/providers           public（可在 login 頁顯示）
  GET  /api/auth/oidc/login/{provider_id} 302 redirect 到 IdP
  GET  /api/auth/oidc/callback?code&state 302 redirect 回前端 with token
  CRUD /api/settings/oidc-providers       org-scoped；admin only

注意：
- /providers 不含 client_secret 等敏感欄位
- 只有「同 org」或 superuser 看得到自己 org 的 OIDC provider
"""
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Cookie, Depends, HTTPException, Query, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.database import get_db
from app.models.oidc_provider import OidcProvider
from app.models.user import User
from app.services.oidc_service import (
    STATE_COOKIE_NAME,
    STATE_COOKIE_TTL,
    build_authorize_url,
    decode_state,
    encode_state,
    exchange_code_for_id_token,
    issue_tokens_for,
    provision_user_from_claims,
)

router = APIRouter()


# ─── Pydantic schemas ─────────────────────────────────────────────────

class OidcPublicProvider(BaseModel):
    """登入頁顯示用：不含敏感欄位。"""
    id: str
    name: str
    button_icon: Optional[str] = None
    button_label: Optional[str] = None


class OidcProviderResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    organization_id: Optional[str] = None
    name: str
    slug: str
    discovery_url: Optional[str] = None
    issuer: Optional[str] = None
    authorize_url: Optional[str] = None
    token_url: Optional[str] = None
    jwks_url: Optional[str] = None
    client_id: str
    client_secret: Optional[str] = None
    scopes: str = "openid email profile"
    button_icon: Optional[str] = None
    button_label: Optional[str] = None
    enabled: bool = False
    created_at: datetime
    updated_at: datetime


class OidcProviderCreate(BaseModel):
    name: str
    slug: str
    client_id: str
    client_secret: Optional[str] = None
    discovery_url: Optional[str] = None
    issuer: Optional[str] = None
    authorize_url: Optional[str] = None
    token_url: Optional[str] = None
    jwks_url: Optional[str] = None
    scopes: Optional[str] = "openid email profile"
    button_icon: Optional[str] = "fa-solid fa-key"
    button_label: Optional[str] = None
    enabled: bool = False


class OidcProviderUpdate(BaseModel):
    name: Optional[str] = None
    slug: Optional[str] = None
    client_id: Optional[str] = None
    client_secret: Optional[str] = None
    discovery_url: Optional[str] = None
    issuer: Optional[str] = None
    authorize_url: Optional[str] = None
    token_url: Optional[str] = None
    jwks_url: Optional[str] = None
    scopes: Optional[str] = None
    button_icon: Optional[str] = None
    button_label: Optional[str] = None
    enabled: Optional[bool] = None


# ─── Public：登入頁取得「可用 SSO 列表」 ──────────────────────────────

@router.get(
    "/auth/oidc/providers",
    response_model=list[OidcPublicProvider],
    tags=["U · 認證"],
)
async def list_public_providers(db: AsyncSession = Depends(get_db)):
    """列出所有 enabled=True 的 OIDC providers；登入頁拿來畫 SSO 按鈕。

    沒做 org 過濾（登入時還不知道使用者屬於哪個 org）；前端會把所有 provider
    都顯示出來，使用者選哪個 IdP 就走哪個 org。
    """
    rows = (
        await db.execute(
            select(OidcProvider).where(OidcProvider.enabled.is_(True)).order_by(OidcProvider.name)
        )
    ).scalars().all()
    return [OidcPublicProvider(
        id=r.id, name=r.name, button_icon=r.button_icon, button_label=r.button_label,
    ) for r in rows]


# ─── 登入啟動 ──────────────────────────────────────────────────────────

def _callback_url(request: Request) -> str:
    """組出 redirect_uri；前端把 host 寫死也可以但這裡直接從 request.url 推。"""
    base = str(request.base_url).rstrip("/")
    return f"{base}/api/auth/oidc/callback"


@router.get("/auth/oidc/login/{provider_id}", tags=["U · 認證"])
async def oidc_login_start(
    provider_id: str,
    request: Request,
    redirect_to: str = Query("/", description="callback 完成後 redirect 回前端的目的地"),
    db: AsyncSession = Depends(get_db),
):
    p = await db.get(OidcProvider, provider_id)
    if not p or not p.enabled:
        raise HTTPException(404, "OIDC provider 不存在或已停用")

    callback = _callback_url(request)
    authorize_url, state, nonce = await build_authorize_url(p, callback)
    state_cookie = encode_state(p.id, state, nonce, redirect_to)

    resp = RedirectResponse(authorize_url, status_code=302)
    resp.set_cookie(
        STATE_COOKIE_NAME,
        state_cookie,
        max_age=STATE_COOKIE_TTL,
        httponly=True,
        secure=False,        # production HTTPS 才設 True
        samesite="lax",
        path="/api/auth/oidc",
    )
    return resp


# ─── Callback ─────────────────────────────────────────────────────────

@router.get("/auth/oidc/callback", tags=["U · 認證"])
async def oidc_callback(
    request: Request,
    code: Optional[str] = Query(None),
    state: Optional[str] = Query(None),
    error: Optional[str] = Query(None),
    error_description: Optional[str] = Query(None),
    state_cookie: Optional[str] = Cookie(None, alias=STATE_COOKIE_NAME),
    db: AsyncSession = Depends(get_db),
):
    if error:
        # IdP 回報錯誤 → 重導回登入頁夾錯
        return RedirectResponse(
            f"/?oidc_error={error}&desc={error_description or ''}", status_code=302
        )
    if not code or not state:
        raise HTTPException(400, "callback 缺少 code / state")
    if not state_cookie:
        raise HTTPException(400, "缺少 state cookie；可能 cookie 過期或被瀏覽器擋下")
    decoded = decode_state(state_cookie)
    if not decoded:
        raise HTTPException(400, "state cookie 無效或過期")
    if decoded.get("state") != state:
        raise HTTPException(400, "state 不符（可能是 CSRF）")

    p = await db.get(OidcProvider, decoded.get("provider_id"))
    if not p or not p.enabled:
        raise HTTPException(404, "OIDC provider 不存在或已停用")

    callback = _callback_url(request)
    try:
        claims = await exchange_code_for_id_token(
            p, code, callback, expected_nonce=decoded.get("nonce") or ""
        )
        user = await provision_user_from_claims(db, p, claims)
        user.last_login_at = datetime.utcnow()
        await db.flush()
        tokens = issue_tokens_for(user)
    except RuntimeError as e:
        return RedirectResponse(f"/?oidc_error=server&desc={str(e)[:200]}", status_code=302)

    # 把 token 透過 URL hash（# 後的部分不會送到 server）回傳給前端
    redirect_to = decoded.get("redirect_to") or "/"
    if "#" in redirect_to:
        redirect_to = redirect_to.split("#")[0]
    target = (
        f"{redirect_to}#oidc_login=1"
        f"&access_token={tokens['access_token']}"
        f"&refresh_token={tokens['refresh_token']}"
    )
    resp = RedirectResponse(target, status_code=302)
    # 清掉 state cookie
    resp.delete_cookie(STATE_COOKIE_NAME, path="/api/auth/oidc")
    return resp


# ─── Admin CRUD（settings tab） ───────────────────────────────────────

def _scope_filter(stmt, user: User):
    if user.is_superuser:
        return stmt
    return stmt.where(OidcProvider.organization_id == user.organization_id)


def _check_org(p: Optional[OidcProvider], user: User) -> OidcProvider:
    if not p:
        raise HTTPException(404, "OIDC provider not found")
    if not user.is_superuser and p.organization_id != user.organization_id:
        raise HTTPException(404, "OIDC provider not found")
    return p


@router.get(
    "/settings/oidc-providers",
    response_model=list[OidcProviderResponse],
    tags=["S · 設定"],
)
async def list_oidc_providers(
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    stmt = select(OidcProvider).order_by(OidcProvider.name)
    stmt = _scope_filter(stmt, user)
    rows = (await db.execute(stmt)).scalars().all()
    return list(rows)


@router.post(
    "/settings/oidc-providers",
    response_model=OidcProviderResponse,
    status_code=201,
    tags=["S · 設定"],
)
async def create_oidc_provider(
    payload: OidcProviderCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # slug 在同 org 內 unique
    existing = (
        await db.execute(
            select(OidcProvider).where(
                OidcProvider.organization_id == user.organization_id,
                OidcProvider.slug == payload.slug,
            )
        )
    ).scalar_one_or_none()
    if existing:
        raise HTTPException(409, f"slug「{payload.slug}」在本組織內已存在")
    p = OidcProvider(
        organization_id=user.organization_id,
        name=payload.name,
        slug=payload.slug,
        client_id=payload.client_id,
        client_secret=payload.client_secret,
        discovery_url=payload.discovery_url,
        issuer=payload.issuer,
        authorize_url=payload.authorize_url,
        token_url=payload.token_url,
        jwks_url=payload.jwks_url,
        scopes=payload.scopes or "openid email profile",
        button_icon=payload.button_icon or "fa-solid fa-key",
        button_label=payload.button_label,
        enabled=payload.enabled,
    )
    db.add(p)
    await db.flush()
    await db.refresh(p)
    return p


@router.put(
    "/settings/oidc-providers/{provider_id}",
    response_model=OidcProviderResponse,
    tags=["S · 設定"],
)
async def update_oidc_provider(
    provider_id: str,
    payload: OidcProviderUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    p = _check_org(await db.get(OidcProvider, provider_id), user)
    data = payload.model_dump(exclude_unset=True)
    for k, v in data.items():
        # client_secret 為空字串 = 不變更（避免 UI 空白覆蓋）
        if k == "client_secret" and v == "":
            continue
        setattr(p, k, v)
    await db.flush()
    await db.refresh(p)
    return p


@router.delete(
    "/settings/oidc-providers/{provider_id}",
    status_code=204,
    tags=["S · 設定"],
)
async def delete_oidc_provider(
    provider_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    p = _check_org(await db.get(OidcProvider, provider_id), user)
    await db.delete(p)
    await db.flush()
