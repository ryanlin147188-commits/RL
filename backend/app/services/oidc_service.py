"""OIDC SSO service — 自製極簡 OIDC client。

為什麼自己寫而不用 Authlib？
- 我們的 provider 是 DB 動態的（每 org 多個），Authlib 的靜態 register 模式不適合
- 流程其實只有 3 步：authorize redirect、token exchange、id_token verify
- 安全性關鍵的部分（JWKS / signature verify）交給 PyJWT 處理，自己只做 orchestration

流程：
1. 前端按「Sign in with X」 → 打 GET /api/auth/oidc/login/{provider_id}
   後端：產生 state/nonce，把 (provider_id, nonce, state) 用 Fernet 加密放進
        cookie 後 302 redirect 到 IdP 的 authorize_url
2. IdP 認證完使用者 → redirect 回 /api/auth/oidc/callback?code=...&state=...
   後端：解密 cookie，驗證 state 一致；用 code 跟 IdP 換 token；驗 id_token
        簽名（透過 JWKS）；建立／找到使用者；發 access+refresh token；
        302 回前端 /#access_token=…&refresh_token=…
"""
from __future__ import annotations

import json
import logging
import secrets
import time
from typing import Optional
from urllib.parse import urlencode

import httpx
import jwt as pyjwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.crypto import _fernet  # 重用同一 Fernet
from app.auth.security import create_access_token, create_refresh_token
from app.models.oidc_provider import OidcProvider
from app.models.user import User

log = logging.getLogger(__name__)

STATE_COOKIE_NAME = "autotest_oidc_state"
STATE_COOKIE_TTL = 600  # 10 分鐘


# ── Discovery cache（每 IdP 一份；TTL 1 小時） ─────────────────────────

_discovery_cache: dict[str, tuple[float, dict]] = {}


async def _fetch_discovery(url: str) -> dict:
    """抓 .well-known/openid-configuration；用 in-memory cache 避免每次都打。"""
    now = time.time()
    cached = _discovery_cache.get(url)
    if cached and now - cached[0] < 3600:
        return cached[1]
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(url)
        r.raise_for_status()
        data = r.json()
    _discovery_cache[url] = (now, data)
    return data


async def _resolve_endpoints(provider: OidcProvider) -> dict[str, str]:
    """從 discovery_url 拉取 authorize/token/jwks endpoint；fallback 到欄位上的手填值。"""
    out = {
        "authorize_url": provider.authorize_url,
        "token_url": provider.token_url,
        "jwks_url": provider.jwks_url,
        "issuer": provider.issuer,
    }
    if provider.discovery_url:
        try:
            disc = await _fetch_discovery(provider.discovery_url)
            out["authorize_url"] = out["authorize_url"] or disc.get("authorization_endpoint")
            out["token_url"] = out["token_url"] or disc.get("token_endpoint")
            out["jwks_url"] = out["jwks_url"] or disc.get("jwks_uri")
            out["issuer"] = out["issuer"] or disc.get("issuer")
        except Exception as e:
            log.warning("OIDC discovery failed for %s: %s", provider.discovery_url, e)
    if not (out["authorize_url"] and out["token_url"] and out["jwks_url"]):
        raise RuntimeError(
            f"OIDC provider「{provider.name}」缺少 endpoint 資訊；請填 discovery_url 或手動填 authorize/token/jwks_url"
        )
    return out


# ── State cookie helpers（Fernet-encrypted JSON） ─────────────────────

def encode_state(provider_id: str, state: str, nonce: str, redirect_to: str) -> str:
    payload = {
        "provider_id": provider_id,
        "state": state,
        "nonce": nonce,
        "ts": time.time(),
        "redirect_to": redirect_to or "/",
    }
    return _fernet.encrypt(json.dumps(payload).encode("utf-8")).decode("ascii")


def decode_state(token: str) -> Optional[dict]:
    try:
        raw = _fernet.decrypt(token.encode("ascii"), ttl=STATE_COOKIE_TTL)
        return json.loads(raw.decode("utf-8"))
    except Exception as e:
        log.warning("OIDC state decode failed: %s", e)
        return None


# ── Authorize URL 組裝 ──────────────────────────────────────────────

async def build_authorize_url(
    provider: OidcProvider, redirect_uri: str
) -> tuple[str, str, str]:
    """回傳 (authorize_url_with_query, state, nonce)。"""
    eps = await _resolve_endpoints(provider)
    state = secrets.token_urlsafe(24)
    nonce = secrets.token_urlsafe(24)
    params = {
        "response_type": "code",
        "client_id": provider.client_id,
        "redirect_uri": redirect_uri,
        "scope": provider.scopes or "openid email profile",
        "state": state,
        "nonce": nonce,
    }
    return f"{eps['authorize_url']}?{urlencode(params)}", state, nonce


# ── Token exchange + id_token verify ────────────────────────────────

async def exchange_code_for_id_token(
    provider: OidcProvider, code: str, redirect_uri: str, expected_nonce: str
) -> dict:
    eps = await _resolve_endpoints(provider)
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.post(
            eps["token_url"],
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": provider.client_id,
                "client_secret": provider.client_secret or "",
            },
            headers={"Accept": "application/json"},
        )
        if r.status_code >= 400:
            raise RuntimeError(f"OIDC token exchange 失敗：{r.status_code} {r.text[:300]}")
        token_resp = r.json()

    id_token = token_resp.get("id_token")
    if not id_token:
        raise RuntimeError("OIDC token response 無 id_token；請確認 scopes 包含 openid")

    # 用 PyJWT 的 PyJWKClient 抓公鑰 + verify
    try:
        jwks_client = pyjwt.PyJWKClient(eps["jwks_url"])
        signing_key = jwks_client.get_signing_key_from_jwt(id_token).key
        claims = pyjwt.decode(
            id_token,
            signing_key,
            algorithms=["RS256", "ES256"],
            audience=provider.client_id,
            issuer=eps["issuer"] if eps.get("issuer") else None,
            options={"verify_iss": bool(eps.get("issuer"))},
        )
    except pyjwt.PyJWTError as e:
        raise RuntimeError(f"id_token 驗證失敗：{e}")

    # 驗 nonce
    if claims.get("nonce") != expected_nonce:
        raise RuntimeError("id_token nonce 不符（可能是 CSRF / replay 攻擊）")

    return claims


# ── User provisioning ──────────────────────────────────────────────

async def provision_user_from_claims(
    db: AsyncSession, provider: OidcProvider, claims: dict
) -> User:
    """根據 id_token claims 找/建 user。

    用 email 當 username（同 org 內 unique 性由 username PK 保證）；email 缺失
    或不允許時 fallback 到 sub。新建的 user 自動歸屬 provider 所屬 org，
    密碼隨機（純 SSO 帳號不能用密碼登入）。
    """
    from app.auth.security import hash_password
    email = claims.get("email") or ""
    sub = claims.get("sub") or ""
    username = (email or sub).strip().lower()
    if not username:
        raise RuntimeError("id_token 缺少 email / sub，無法決定 username")

    user = (await db.execute(select(User).where(User.username == username))).scalar_one_or_none()
    if user:
        # 已存在 → 確保 active
        if not user.is_active:
            raise RuntimeError(f"使用者「{username}」已被停用")
        return user

    # 新建：密碼 = 隨機 32 字元（不會洩漏，因為 SSO 流程繞過密碼登入）
    user = User(
        username=username,
        display_name=claims.get("name") or claims.get("preferred_username") or username,
        email=email or None,
        password_hash=hash_password(secrets.token_urlsafe(32)),
        organization_id=provider.organization_id,
        is_active=True,
        is_superuser=False,
    )
    db.add(user)
    await db.flush()
    await db.refresh(user)
    return user


def issue_tokens_for(user: User) -> dict:
    """登入成功後發 access + refresh token，與 password login 相容的格式。"""
    extra = {"org_id": user.organization_id, "is_superuser": user.is_superuser}
    return {
        "access_token": create_access_token(user.username, extra=extra),
        "refresh_token": create_refresh_token(user.username),
    }
