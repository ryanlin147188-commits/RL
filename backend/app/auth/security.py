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
from passlib.context import CryptContext

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

# bcrypt 設定（passlib 會自動處理 salt）
_pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")


# ── 密碼 ──────────────────────────────────────────────────────────────

def hash_password(plain: str) -> str:
    return _pwd_ctx.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    if not plain or not hashed:
        return False
    try:
        return _pwd_ctx.verify(plain, hashed)
    except Exception:
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

    Dual-mode(Phase 1):當 ``CASDOOR_ENABLED=True`` 時先試 RS256 + JWKS
    (Casdoor 簽的 OIDC token),失敗再 fall back 到 HS256(本地舊 token)。
    Cutover(Phase 4)完成後可把 HS256 fallback 拔掉,只留 RS256 路徑。

    為什麼順序是 RS256 → HS256:Casdoor token 用 RS256 簽且帶有 `kid` header,
    HS256 解時 PyJWT 會直接 raise InvalidAlgorithmError;反過來 HS256 token
    沒 kid,Casdoor 路徑會在 ``get_signing_key_from_jwt`` 階段就拋,額外網路
    成本只發生在「未認 Casdoor JWT 卻誤丟進來」的小機率,可接受。
    """
    # Lazy import — 避免 CASDOOR_ENABLED=False 部署在 import time 即觸發
    # Casdoor 模組初始化(它會讀 env / 準備 JWKS client URL)。
    from app.auth import casdoor as _casdoor

    if _casdoor.is_enabled():
        try:
            return _casdoor.decode_casdoor_jwt(token)
        except jwt.PyJWTError:
            # 不是 Casdoor 簽的 token,或 Casdoor 暫時不可達 → 再試 HS256
            pass
    return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
