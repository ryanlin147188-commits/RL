"""JWT 簽發 / 驗證 + bcrypt 密碼雜湊。

JWT 用對稱 HS256;簽章用 AUTOTEST_JWT_SECRET 環境變數,**必填**。
deploy.sh / deploy.ps1 首次啟動會自動產生隨機值寫入 .env;手動部署時請設足夠長的隨機字串。

Tokens 內容:
- sub: username
- exp: 過期時間(UTC timestamp)
- iat: 簽發時間
- typ: "access" | "refresh"
"""
from __future__ import annotations

import os
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
        "If you used deploy.sh / deploy.ps1, this should have been generated automatically."
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
        "iat": _now_utc(),
        "exp": _now_utc() + timedelta(days=REFRESH_TOKEN_TTL_DAYS),
        "typ": "refresh",
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_token(token: str) -> dict:
    """成功 → payload dict；失敗 → 拋 jwt.* 子例外。
    呼叫端 (middleware / dependency) 自行 catch 並回 401。"""
    return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
