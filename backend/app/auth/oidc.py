"""In-process OIDC client(v1.1.7,httpx-oauth + httpx 後端)。

v1.1.3–v1.1.4 委派 OIDC 給 Casdoor sidecar;v1.1.5 改成 backend 自己拿
``authlib`` 跟 IdP 做 OAuth2 code flow。v1.1.7 Phase 6 進一步換成
``httpx-oauth`` — fastapi-users 親緣的 OAuth2 client,以後若要走
fastapi-users 內建的 OAuth router 比較好接(authlib 跟 fastapi-users 沒
直接整合)。功能等價,API 表面差不多,差別只在 dependency 來源。

多 provider 設計:每個 provider 一份 :class:`OIDCProvider` dataclass,通過
``PROVIDERS`` dict 暴露;現階段只啟用 Zoho,要加 Google / Microsoft 等只是
複製貼上加 30 行 config(不需改 router)。

API 蓋掉的取捨:

* httpx-oauth 提供 ``BaseOAuth2`` 通用 client,我們不用它的 ``OpenID`` 子類
  因為 Zoho 不走 id_token,只 fetch /oauth/user/info(userinfo endpoint)。
* state cookie 仍由我們自家 HS256 簽,httpx-oauth 不管 state 持久化。
* userinfo 拉一次,JIT provision 時用 ``sub``(stable)當 key。
"""
from __future__ import annotations

import logging
import os
import secrets
from dataclasses import dataclass
from typing import Any, Optional

import httpx
from httpx_oauth.oauth2 import BaseOAuth2, GetAccessTokenError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OIDCProvider:
    """單一 OIDC IdP 設定。所有欄位都從 env 取,空字串 = 未啟用。"""
    name: str            # internal key,例:``zoho``
    display_name: str    # UI 顯示用,例:``Zoho``
    client_id: str
    client_secret: str
    auth_url: str
    token_url: str
    userinfo_url: str
    scope: str
    redirect_uri: str    # backend callback URL,必須跟 IdP dev console 填的一致

    def is_enabled(self) -> bool:
        return bool(self.client_id and self.client_secret)


# ── Zoho ───────────────────────────────────────────────────────────────
# 預設 region 是 ``accounts.zoho.com``(global / US);.eu / .in / .com.au /
# .jp / .com.cn 用 ``ZOHO_OIDC_BASE`` 覆蓋整個 base URL。

_ZOHO_BASE = os.environ.get("ZOHO_OIDC_BASE", "https://accounts.zoho.com").rstrip("/")

ZOHO = OIDCProvider(
    name="zoho",
    display_name="Zoho",
    client_id=os.environ.get("ZOHO_CLIENT_ID", "").strip(),
    client_secret=os.environ.get("ZOHO_CLIENT_SECRET", "").strip(),
    auth_url=f"{_ZOHO_BASE}/oauth/v2/auth",
    token_url=f"{_ZOHO_BASE}/oauth/v2/token",
    userinfo_url=f"{_ZOHO_BASE}/oauth/user/info",
    # AaaServer.profile.READ 需要才能拉 /oauth/user/info 拿 Display_Name / Email。
    # openid 在 Zoho OIDC 端也吃,但 id_token 我們不靠 — JIT 走 userinfo 即可。
    scope="AaaServer.profile.READ email openid",
    redirect_uri=os.environ.get(
        "ZOHO_REDIRECT_URL",
        "http://localhost/api/auth/zoho/callback",
    ).strip(),
)


# Registry:所有 provider 一覽。要加新 IdP 時新增一份 OIDCProvider 後在這
# 註冊 key。router 一律走 ``get_provider(name)`` 拿 config,不直接 import
# 個別 constant,讓擴充點集中。
PROVIDERS: dict[str, OIDCProvider] = {
    ZOHO.name: ZOHO,
}


def get_provider(name: str) -> Optional[OIDCProvider]:
    return PROVIDERS.get(name)


def is_enabled(name: str) -> bool:
    p = PROVIDERS.get(name)
    return bool(p and p.is_enabled())


# ── OAuth helpers ──────────────────────────────────────────────────────


def _make_oauth_client(provider: OIDCProvider) -> BaseOAuth2:
    """為單一 OIDC provider 建一個 httpx-oauth ``BaseOAuth2`` client。

    每次 callback 都建一個新的(輕量,沒有開連線);request 時 httpx 內部
    用 ContextManager 開 socket、結束 close。
    """
    return BaseOAuth2(
        client_id=provider.client_id,
        client_secret=provider.client_secret,
        authorize_endpoint=provider.auth_url,
        access_token_endpoint=provider.token_url,
        # Zoho 不公開 refresh / revoke endpoint;我們也不靠它的 refresh
        # token(每次登入 backend mint 自家 HS256)。設 None。
        refresh_token_endpoint=None,
        revoke_token_endpoint=None,
        # 預設 scope 帶 provider 上設的 string,httpx-oauth 預期 list。
        base_scopes=[s for s in (provider.scope or "").split() if s],
    )


async def build_authorize_url(provider: OIDCProvider, state: str) -> str:
    """組 IdP 的 authorize URL(browser 會被 302 過去)。

    httpx-oauth 的 :meth:`BaseOAuth2.get_authorization_url` 內部會自動把
    ``response_type=code``、``client_id``、``redirect_uri``、``scope``、
    ``state`` 全部 urlencode 進去 — 比手工 urlencode 少出 bug。
    """
    client = _make_oauth_client(provider)
    return await client.get_authorization_url(
        redirect_uri=provider.redirect_uri,
        state=state,
        scope=[s for s in (provider.scope or "").split() if s] or None,
    )


async def exchange_code_for_token(provider: OIDCProvider, code: str) -> dict[str, Any]:
    """authorization code → access_token。回 dict 包含 ``access_token`` /
    ``token_type``(Zoho 回 ``Bearer``)/ 可能還有 ``expires_in`` / ``id_token``。

    httpx-oauth 的 :class:`OAuth2Token` 是 ``TypedDict``-like,直接 dict()
    轉成 plain dict 給 caller。錯誤包成 ``RuntimeError`` 維持跟舊 authlib
    版本同樣的 exception type,免動 caller try/except。
    """
    client = _make_oauth_client(provider)
    try:
        token = await client.get_access_token(code, provider.redirect_uri)
    except GetAccessTokenError as exc:
        raise RuntimeError(f"{provider.name} token exchange failed: {exc}") from exc
    except Exception as exc:
        # httpx 連線層級錯誤等;統一回 RuntimeError。
        raise RuntimeError(f"{provider.name} token exchange failed: {exc}") from exc
    return dict(token)


async def fetch_userinfo(provider: OIDCProvider, access_token: str) -> dict[str, Any]:
    """userinfo endpoint → 使用者 claims dict(provider 特定欄位名)。

    Zoho 回類似:
    ``{"Display_Name":"...", "Email":"...", "First_Name":"...",
       "Last_Name":"...", "ZUID": <int>, ...}``
    """
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get(
            provider.userinfo_url,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if r.status_code >= 400:
            raise RuntimeError(
                f"{provider.name} userinfo failed: HTTP {r.status_code} {r.text[:300]}"
            )
        return r.json()


def normalize_claims(provider: OIDCProvider, raw: dict[str, Any]) -> dict[str, Any]:
    """把 provider 特定的 claim 名稱對齊到內部 schema:
    ``{sub, email, display_name}``。其他 provider 加進來時在這加 branch。"""
    if provider.name == "zoho":
        # Zoho ``ZUID`` 是 int,轉 str 一致以 string key 存 DB。
        zuid = raw.get("ZUID")
        sub = str(zuid) if zuid not in (None, "") else (raw.get("Email") or "")
        return {
            "sub": sub,
            "email": (raw.get("Email") or "").strip().lower() or None,
            "display_name": (
                raw.get("Display_Name")
                or " ".join(
                    s for s in [raw.get("First_Name"), raw.get("Last_Name")] if s
                ).strip()
                or None
            ),
        }
    # Generic OIDC defaults
    return {
        "sub": str(raw.get("sub") or "").strip(),
        "email": (raw.get("email") or "").strip().lower() or None,
        "display_name": (raw.get("name") or raw.get("preferred_username") or "").strip() or None,
    }


def make_state(length: int = 24) -> str:
    """產生 OAuth state(CSRF token)。簽章 + cookie 保存的部分由 caller(router)
    處理 — 這裡只負責產生足夠 entropy 的字串。"""
    return secrets.token_urlsafe(length)
