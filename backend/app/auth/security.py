"""JWT 簽發 / 驗證 + bcrypt 密碼雜湊。

JWT 用對稱 HS256;簽章用 AUTOTEST_JWT_SECRET 環境變數,**必填**。
首次部署可用 `docker compose --profile init run --rm bootstrap` 自動產生隨機值
寫入 .env;手動部署時請設足夠長的隨機字串(建議 `openssl rand -hex 32`)。

Tokens 內容:
- sub: username
- exp: 過期時間(UTC timestamp)
- iat: 簽發時間
- typ: "access" | "refresh"
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt
from fastapi_users.password import PasswordHelper

# ── 設定 ──────────────────────────────────────────────────────────────
# 環境變數必填:無 fallback。任何 fallback 字串都會被 codebase 公開,等同無認證。
_jwt_secret_raw = os.environ.get("AUTOTEST_JWT_SECRET", "").strip()
if not _jwt_secret_raw:
    raise RuntimeError(
        "AUTOTEST_JWT_SECRET environment variable is required. "
        "Generate a random value (e.g., `openssl rand -hex 32`) and set it in your .env file. "
        "Or run `docker compose --profile init run --rm bootstrap` to auto-generate "
        "a complete .env (creates only if missing)."
    )
JWT_SECRET: str = _jwt_secret_raw
JWT_ALGORITHM: str = "HS256"
ACCESS_TOKEN_TTL_MINUTES: int = int(os.environ.get("AUTOTEST_ACCESS_TTL_MIN", "120"))   # 2h
REFRESH_TOKEN_TTL_DAYS: int = int(os.environ.get("AUTOTEST_REFRESH_TTL_DAYS", "14"))    # 14d

# v1.1.7 Phase 4: 密碼 hash 內部走 fastapi-users PasswordHelper(pwdlib +
# bcrypt 4.x backend)。public API hash_password / verify_password 不變,30+
# callsite 不用改。bcrypt $2b$ hash 格式跨 lib 相容,既有 password_hash 不必
# rehash。passlib import 已拔掉。
_password_helper = PasswordHelper()


# ── 密碼 ──────────────────────────────────────────────────────────────

def hash_password(plain: str) -> str:
    return _password_helper.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    if not plain or not hashed:
        return False
    try:
        verified, _maybe_new_hash = _password_helper.verify_and_update(plain, hashed)
        # verify_and_update 第二個值 = 若 hash 該升級成更強演算法時的新 hash。
        # 我們不在這支接口提供 rehash;Phase 5 cutover 後若想啟用,改在
        # UserManager.on_after_login 裡持久化新 hash。
        return bool(verified)
    except Exception:  # noqa: BLE001
        return False


# ── JWT ───────────────────────────────────────────────────────────────

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def create_access_token(username: str, extra: Optional[dict] = None) -> str:
    payload = {
        "sub": username,
        "jti": uuid.uuid4().hex,
        "iat": _now_utc(),
        "exp": _now_utc() + timedelta(minutes=ACCESS_TOKEN_TTL_MINUTES),
        "typ": "access",
    }
    if extra:
        payload.update(extra)
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def create_refresh_token(username: str) -> str:
    payload = {
        "sub": username,
        "jti": uuid.uuid4().hex,
        "iat": _now_utc(),
        "exp": _now_utc() + timedelta(days=REFRESH_TOKEN_TTL_DAYS),
        "typ": "refresh",
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


# ── Active organisation cookie ──────────────────────────────────────────
# Casdoor JWT 只負責「身分」(sub / email / Casdoor role),刻意不在 access
# token 裡放 org_id — 因為使用者可同時屬於多個 org,需要客戶端決定當前在哪
# 個 org 操作。把 active_org_id 放在一個我們自己簽章的 short-lived cookie
# 裡,middleware 解出後 push 到 ContextVar 給 tenant scoping 使用。
#
# 用 HS256 + AUTOTEST_JWT_SECRET 簽:跟既有 access token 共用 secret 但 typ
# 不同(``active_org``),所以即使被誤丟進 Authorization header 也不會通過
# middleware 的 ``typ == "access"`` 檢查。

ACTIVE_ORG_COOKIE_NAME: str = "active_org_id"
ACTIVE_ORG_COOKIE_TTL_DAYS: int = REFRESH_TOKEN_TTL_DAYS  # 對齊 refresh token

# Cookie Secure flag 判定 — 解決「實際走 HTTPS 但 backend 看到 http scheme」問題
#
# 背景:典型部署是 ``browser → nginx(443) → gateway → backend(http)``。Backend
# 的 ``request.url.scheme`` 永遠是 ``http``,如果直接拿來決定 ``Secure``,
# Set-Cookie 永遠不帶 Secure flag,等於把 token 暴露在「萬一誤打 HTTP」時的
# MitM 風險下。
#
# 解法:Production 一律強制 ``Secure=True``。Production 判定如下:
#   1. 環境變數 ``AUTOTEST_FORCE_SECURE_COOKIES=1`` → 強制 True
#   2. ``settings.BASE_URL`` 是 ``https://...`` → True
#   3. ``request.url.scheme == "https"`` 或 X-Forwarded-Proto=https → True
#   4. 其他(純內網 / 開發機 / localhost)→ False
#
# 注意:gateway 已經有設 X-Forwarded-Proto,但 backend uvicorn 沒裝
# ``--proxy-headers`` → 看不到。我們直接讀 header 自己處理,避免改 entrypoint。
_FORCE_SECURE_COOKIES = os.environ.get("AUTOTEST_FORCE_SECURE_COOKIES", "").strip() in ("1", "true", "yes")


def should_use_secure_cookie(request) -> bool:
    """Determine whether Set-Cookie should carry the ``Secure`` flag.

    See module docstring above for the decision tree. Pass the FastAPI/Starlette
    ``Request`` so we can inspect headers + scheme — but DO NOT trust
    ``request.url.scheme`` alone behind a reverse proxy.
    """
    if _FORCE_SECURE_COOKIES:
        return True
    try:
        from app.config import settings as _settings
        if (_settings.BASE_URL or "").lower().startswith("https://"):
            return True
    except Exception:  # noqa: BLE001
        pass
    if request is None:
        return False
    # X-Forwarded-Proto 由 gateway 設;只信 gateway 經手的流量
    proto = (request.headers.get("x-forwarded-proto") or "").lower()
    if proto == "https":
        return True
    return request.url.scheme == "https"


def sign_active_org_cookie(username: str, org_id: str) -> str:
    payload = {
        "sub": username,
        "org": org_id,
        "iat": _now_utc(),
        "exp": _now_utc() + timedelta(days=ACTIVE_ORG_COOKIE_TTL_DAYS),
        "typ": "active_org",
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def verify_active_org_cookie(cookie_value: str, *, expected_sub: Optional[str] = None) -> Optional[str]:
    """成功 → 回 org_id;簽章/過期/typ/sub 任一不符 → 回 None。

    不拋例外是因為 caller 都在 hot path(middleware),失敗時就 fall back
    到 JWT 內的 org_id 或 None,不需要 401 而中斷整個 request。

    ``expected_sub`` 帶入時會比對 cookie 的 ``sub`` 是否相同 — 防止別人偷另
    一個帳號的 cookie 接到自己的 JWT 上來做 cross-account org 穿透。
    """
    if not cookie_value:
        return None
    try:
        payload = jwt.decode(cookie_value, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.PyJWTError:
        return None
    if payload.get("typ") != "active_org":
        return None
    if expected_sub is not None and payload.get("sub") != expected_sub:
        return None
    org_id = payload.get("org")
    return org_id if isinstance(org_id, str) and org_id else None


def decode_token(token: str) -> dict:
    """成功 → payload dict；失敗 → 拋 jwt.* 子例外。
    呼叫端 (middleware / dependency) 自行 catch 並回 401。

    v1.1.5 起本端只簽 HS256 token(密碼登入 + Zoho OIDC callback 都走 backend
    自己 mint 的 HS256)。Casdoor cutover 期間的 RS256 + JWKS dual-mode 已
    移除,callback 改成 backend in-process 跟 IdP 換 token 後再簽 HS256,
    所有後續 request 都看自家 secret。
    """
    return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
