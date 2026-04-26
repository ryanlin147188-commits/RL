"""JWT 簽發 / 驗證 + bcrypt 密碼雜湊。

JWT 用對稱 HS256；簽章用 settings.JWT_SECRET (預設 fallback 到 "change-me-in-prod")。
正式部署應透過環境變數設定一個夠長的隨機字串。

Tokens 內容：
- sub: username
- exp: 過期時間（UTC timestamp）
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
# 從環境變數讀；沒有就用 fallback（dev only）
JWT_SECRET: str = os.environ.get("AUTOTEST_JWT_SECRET") or "change-me-in-production-please-use-a-long-random-string"
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
