"""In-process OIDC client(v1.1.5,authlib + httpx 後端)。

v1.1.3–v1.1.4 委派 OIDC 給 Casdoor sidecar 走完,中間踩過 subpath SPA 白屏 /
session config 各種坑;v1.1.5 改成 backend 自己拿 ``authlib`` 跟 IdP 做
OAuth2 code flow,Casdoor sidecar 完全下架。

多 provider 設計:每個 provider 一份 :class:`OIDCProvider` dataclass,通過
``PROVIDERS`` dict 暴露;現階段只啟用 Zoho,要加 Google / Microsoft 等只是
複製貼上加 30 行 config(不需改 router)。

API 蓋掉的取捨:

* authlib 提供 ``OAuth`` registry + ``AsyncOAuth2Client``;我們選 OAuth2Client
  繞開 Starlette session 依賴(``OAuth`` 需要 SessionMiddleware),改用我們
  自家 HS256 cookie 簽 state。
* ID token 驗章透過 IdP 的 JWKS;authlib 內建快取。
* userinfo 拉一次,JIT provision 時用 ``sub``(stable)當 key。
"""
from __future__ import annotations

import logging
import os
import secrets
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urlencode

import httpx
from authlib.integrations.httpx_client import AsyncOAuth2Client

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


def build_authorize_url(provider: OIDCProvider, state: str) -> str:
    """組 IdP 的 authorize URL(browser 會被 302 過去)。"""
    params = {
        "response_type": "code",
        "client_id": provider.client_id,
        "redirect_uri": provider.redirect_uri,
        "scope": provider.scope,
        "state": state,
        # Zoho OIDC 要 ``access_type=offline`` 才會發 refresh_token,但我們
        # 不需要 — 每次登入 backend mint 自家 HS256,IdP 的 refresh 沒人用。
        # 不加 ``prompt=consent`` 讓使用者第二次登入時不必再點同意。
    }
    return f"{provider.auth_url}?{urlencode(params)}"


async def exchange_code_for_token(provider: OIDCProvider, code: str) -> dict[str, Any]:
    """authorization code → access_token。回 dict 包含 ``access_token`` /
    ``token_type``(Zoho 回 ``Bearer``)/ 可能還有 ``expires_in`` / ``id_token``。"""
    async with AsyncOAuth2Client(
        client_id=provider.client_id,
        client_secret=provider.client_secret,
        token_endpoint=provider.token_url,
        timeout=15.0,
    ) as client:
        try:
            token = await client.fetch_token(
                provider.token_url,
                grant_type="authorization_code",
                code=code,
                redirect_uri=provider.redirect_uri,
            )
        except Exception as e:  # authlib raises various OAuth-specific errors
            raise RuntimeError(f"{provider.name} token exchange failed: {e}") from e
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
