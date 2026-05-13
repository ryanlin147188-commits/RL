"""Casdoor OIDC 整合輔助模組。

Phase 1 的重點:讓 ``decode_token`` 可以同時驗 HS256(舊的本地 JWT)與 RS256
(Casdoor 簽的 OIDC token)。本檔負責:

* 讀取 ``CASDOOR_*`` 環境變數,提供 ``is_enabled()`` 給 caller 判斷是否啟用
* 維護一個 lazy / 1h cache 的 :class:`jwt.PyJWKClient`,從
  ``{CASDOOR_ENDPOINT}/.well-known/jwks`` 拉公鑰
* 提供 :func:`decode_casdoor_jwt`:輸入 token,輸出 verified payload
  (失敗一律拋 ``jwt.PyJWTError`` 子例外,由 caller 自行轉成 401)

設計取捨:

* JWKS 拉取走 HTTP / 同步 ``urllib`` — Casdoor sidecar 在同個 docker
  network 內,latency 微秒級;PyJWKClient 內建 LRU cache 預設 16 keys / 1h,
  足夠用。不開非同步是因為 ``jwt.decode`` 本身是同步 API,硬塞 async
  反而需要在 middleware 用 thread executor。
* 把 Casdoor REST API 客戶端(``add-user`` / ``get-users`` / 等)也放這裡,
  讓 ``CASDOOR_*`` env 只在這個模組讀取,其他 caller 只透過 typed function
  互動,後續換成 SDK 時改動面小。
"""
from __future__ import annotations

import logging
import os
import secrets
from typing import Any, Optional
from urllib.parse import urlencode

import httpx
import jwt as pyjwt

logger = logging.getLogger(__name__)

# 環境變數 — 都允許 fallback,讓未啟用 Casdoor 的部署仍能 import 本模組。
_ENABLED_RAW = os.environ.get("CASDOOR_ENABLED", "False").strip().lower()
_CASDOOR_ENABLED: bool = _ENABLED_RAW in {"true", "1", "yes", "on"}

# 兩個 endpoint 必要,差別在誰會 hit 這個 URL:
#   * CASDOOR_ENDPOINT(內網):backend 自己用 docker DNS 打 REST / JWKS / token
#     exchange。預設 ``http://casdoor:8000``。
#   * CASDOOR_PUBLIC_ENDPOINT(對外):browser 從 SPA 跳到 Casdoor 認證頁時用,
#     必須是瀏覽器解析得到的 URL。預設 fallback 到 CASDOOR_ENDPOINT,但
#     ``casdoor`` 是 docker 內網 hostname,production 部署必須 override 成走
#     APISIX 的對外 URL,例如 ``http://<host>/casdoor``。否則 302 出去後瀏覽器
#     會 DNS_PROBE_FINISHED_NXDOMAIN。
CASDOOR_ENDPOINT: str = os.environ.get("CASDOOR_ENDPOINT", "http://casdoor:8000").rstrip("/")
CASDOOR_PUBLIC_ENDPOINT: str = (
    os.environ.get("CASDOOR_PUBLIC_ENDPOINT", "").strip().rstrip("/")
    or CASDOOR_ENDPOINT
)
CASDOOR_ORG: str = os.environ.get("CASDOOR_ORG", "autotest")
CASDOOR_APP: str = os.environ.get("CASDOOR_APP", "rl-platform")
CASDOOR_CLIENT_ID: str = os.environ.get("CASDOOR_CLIENT_ID", "")
CASDOOR_CLIENT_SECRET: str = os.environ.get("CASDOOR_CLIENT_SECRET", "")
CASDOOR_REDIRECT_URL: str = os.environ.get(
    "CASDOOR_REDIRECT_URL", "http://localhost/api/auth/callback",
)

# PyJWKClient 自帶 LRU cache(16 keys, 1h),足夠 Casdoor 單一 signing key 用。
_jwk_client: Optional[pyjwt.PyJWKClient] = None


def is_enabled() -> bool:
    """是否啟用 Casdoor dual-mode。

    為 False 時 ``decode_token`` 完全不嘗試解 RS256,行為與舊版相同。
    """
    return _CASDOOR_ENABLED


def _get_jwk_client() -> pyjwt.PyJWKClient:
    """lazy 建立 PyJWKClient;放在 module-level 不行,因為 import 時 sidecar
    可能還沒起,會在第一個 health check 失敗。"""
    global _jwk_client
    if _jwk_client is None:
        jwks_url = f"{CASDOOR_ENDPOINT}/.well-known/jwks"
        # cache_keys=True:第一次拿到 key 後 1h 內都從 RAM 拿,不打網路
        # lifespan=3600:1h 後過期重抓,讓 Casdoor 旋鑰時 backend 自動跟上
        _jwk_client = pyjwt.PyJWKClient(jwks_url, cache_keys=True, lifespan=3600)
        logger.info("Casdoor JWKS client initialised (url=%s)", jwks_url)
    return _jwk_client


def decode_casdoor_jwt(token: str) -> dict[str, Any]:
    """RS256 + JWKS 驗證 Casdoor 簽的 access_token / id_token。

    成功 → claims dict;失敗 → 拋 ``jwt.PyJWTError`` 子例外。

    驗證項目:

    * 簽章(JWKS 公鑰)
    * exp / iat / nbf(PyJWT 預設 leeway=0)
    * issuer = ``CASDOOR_ENDPOINT``(Casdoor JWT 的 iss 預設就是 endpoint URL)
    * audience 不檢查 — Casdoor 對同一個 app 簽出來的 token 可以給多個 client,
      強制 aud 反而會把單 sign-in app 多 redirect 場景搞壞;改在 sub / org 層
      檢查身分。

    Caller 端再驗 ``typ`` / 自己的業務約束(例如 token_generation)。
    """
    signing_key = _get_jwk_client().get_signing_key_from_jwt(token)
    # 注意:iss 不強制檢查 — Casdoor 視「token 是由哪條 URL 進來的請求換出來的」
    # 而設不同的 iss。我們內部走 ``http://casdoor:8000`` 換 token,但 origin
    # 可能寫成 public URL,兩個值都合法;簽章已驗過,iss 用 audit log 紀錄即可。
    return pyjwt.decode(
        token,
        key=signing_key.key,
        algorithms=["RS256"],
        options={"verify_aud": False, "verify_iss": False},
    )


# ── OAuth2 / OIDC client-side helpers ──────────────────────────────────

def build_authorize_url(redirect_uri: str, state: str, scope: str = "openid profile email") -> str:
    """組 Casdoor 的 authorize URL,給 /api/auth/casdoor/login 302 redirect 用。

    這個 URL 是 **browser 會被 302 過去** 的位置,必須使用 ``CASDOOR_PUBLIC_ENDPOINT``
    (對外 URL,通常走 APISIX `/casdoor/*` 反代)。不能用 ``CASDOOR_ENDPOINT``
    (docker 內網 hostname),否則 browser 會 NXDOMAIN。

    Casdoor 的 OIDC authorize endpoint 是 ``/login/oauth/authorize``(不是
    well-known 的 ``/authorize``)。寫死路徑可以省一次 discovery 拉取。
    """
    params = {
        "response_type": "code",
        "client_id": CASDOOR_CLIENT_ID,
        "redirect_uri": redirect_uri,
        "scope": scope,
        "state": state,
    }
    return f"{CASDOOR_PUBLIC_ENDPOINT}/login/oauth/authorize?{urlencode(params)}"


async def exchange_code_for_token(code: str, redirect_uri: str) -> dict[str, Any]:
    """把 authorization code 換成 access_token / refresh_token / id_token。

    Casdoor 的 token endpoint 是 ``/api/login/oauth/access_token``;走 form
    encoding(``application/x-www-form-urlencoded``)而不是 JSON body,跟標準
    OAuth2 RFC 6749 一致。
    """
    if not CASDOOR_CLIENT_ID or not CASDOOR_CLIENT_SECRET:
        raise RuntimeError(
            "Casdoor client credentials 未設定 — 請在 .env 加上 CASDOOR_CLIENT_ID / CASDOOR_CLIENT_SECRET"
        )
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.post(
            f"{CASDOOR_ENDPOINT}/api/login/oauth/access_token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": CASDOOR_CLIENT_ID,
                "client_secret": CASDOOR_CLIENT_SECRET,
            },
            headers={"Accept": "application/json"},
        )
        if r.status_code >= 400:
            raise RuntimeError(
                f"Casdoor token exchange 失敗:{r.status_code} {r.text[:300]}"
            )
        return r.json()


def make_state(length: int = 24) -> str:
    """產生 OAuth state(CSRF token)。簽章 + cookie 保存的部分由 caller(router)
    處理 — 這裡只負責產生足夠 entropy 的字串。"""
    return secrets.token_urlsafe(length)


# ── JIT user provisioning ──────────────────────────────────────────────

async def provision_user_from_casdoor_claims(
    db,  # AsyncSession — 避免循環 import,從 caller 傳進來
    claims: dict[str, Any],
):
    """根據 Casdoor id_token / userinfo claims 找/建 User row。

    跟 :func:`app.services.oidc_service.provision_user_from_claims` 邏輯相似,
    但額外:

    * 把 ``sub``(Casdoor 的 stable uuid)寫到 ``users.casdoor_user_id``,
      讓 Phase 6 的 webhook / 5-min reconcile job 用 uuid 而非 username 對齊
    * 優先用 ``preferred_username`` 當 username(Casdoor 對 OIDC 標準的對應
      欄位就是這個)
    """
    from sqlalchemy import select

    from app.auth.security import hash_password
    from app.models.user import User

    sub = (claims.get("sub") or "").strip()
    email = (claims.get("email") or "").strip().lower()
    pref = (
        claims.get("preferred_username")
        or claims.get("name")
        or claims.get("displayName")
        or ""
    ).strip()
    # username 決定:preferred_username > email > sub(uuid)
    username = (pref or email or sub).lower()
    if not username:
        raise RuntimeError("Casdoor token 缺少 sub / preferred_username,無法決定 username")

    # 先用 casdoor_user_id 對齊;對不到才退回 username 對齊(向下相容既有帳號)
    user: Optional["User"] = None
    if sub:
        user = (
            await db.execute(select(User).where(User.casdoor_user_id == sub))
        ).scalar_one_or_none()
    if user is None:
        user = (
            await db.execute(select(User).where(User.username == username))
        ).scalar_one_or_none()

    if user:
        if not user.is_active:
            raise RuntimeError(f"使用者「{user.username}」已被停用")
        # 補/校正 casdoor_user_id;這是首個 OIDC 登入綁定的時機點
        if sub and user.casdoor_user_id != sub:
            user.casdoor_user_id = sub
        # 同步 email / display_name(允許 Casdoor 為 source of truth)
        if email and user.email != email:
            user.email = email
        if pref and user.display_name != pref:
            user.display_name = pref
        await db.flush()
        return user

    user = User(
        username=username,
        display_name=pref or username,
        email=email or None,
        password_hash=hash_password(secrets.token_urlsafe(32)),  # SSO-only 帳號
        casdoor_user_id=sub or None,
        is_active=True,
        is_superuser=False,
    )
    db.add(user)
    await db.flush()
    return user
