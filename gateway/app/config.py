"""Gateway settings — 從環境變數讀,啟動時 freeze。

關鍵 secret:
* ``AUTOTEST_JWT_SECRET`` — 跟 backend 同一份,gateway 用它 decode 上行 JWT。
* ``GATEWAY_BACKEND_SHARED_SECRET`` — 只有 gateway / backend 兩邊知道。Gateway 用
  HMAC-SHA256 簽 ``X-Gateway-Verified`` header,backend 端 ``AuthMiddleware``
  看到合法簽章就跳過 JWT decode,直接拿 ``X-Gateway-User`` / ``X-Gateway-Org``
  重組 ``request.state.user_payload``。沒這個 secret 就退回原本的雙層獨立驗證。
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


_WEAK_SECRETS = {"changeme", "secret", "password", "admin123", ""}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore")

    # ── 上游 ────────────────────────────────────────────────
    backend_url: str = "http://backend:8000"

    # ── JWT(跟 backend 共享)─────────────────────────────
    autotest_jwt_secret: str
    # decode_access_token_payload 預設 HS256;backend OIDC RS256 不會走 gateway
    # 短路(那條會被當「無 secret 認的 token」直接 forward,由 backend 自己驗)
    jwt_algorithm: str = "HS256"

    # ── Gateway ↔ Backend HMAC ─────────────────────────
    # v1.1.13:從 Optional[str]=None 改為強制必填。無 secret 等於信任邊界破洞,
    # 攻擊者繞 gateway 直打 backend 可跳過限流 / revocation 檢查(若 backend
    # 開了 BACKEND_TRUST_GATEWAY_ONLY 則完全不擋)。
    gateway_backend_shared_secret: str = Field(..., min_length=32)
    # HMAC timestamp tolerance(秒)— 超過就拒,防 replay
    hmac_timestamp_tolerance_seconds: int = 30

    @field_validator("gateway_backend_shared_secret")
    @classmethod
    def _reject_weak_shared_secret(cls, v: str) -> str:
        if v.strip().lower() in _WEAK_SECRETS:
            raise ValueError(
                "GATEWAY_BACKEND_SHARED_SECRET 不可為已知弱值,"
                "請用 `openssl rand -hex 32` 產生隨機 secret"
            )
        return v

    # ── CORS ────────────────────────────────────────────────
    # 跟 backend 同一份 env,gateway 接管 CORS handling
    allowed_origins: str = "http://localhost"

    # ── 限速 storage ───────────────────────────────────────
    # in-memory:單實例 OK;移到 Valkey/Redis 才能多實例共享 quota
    redis_url: Optional[str] = "redis://valkey:6379/2"

    # ── 動態 routes ────────────────────────────────────────
    routes_yaml_path: str = "/app/routes.yaml"

    # ── 觀測 ────────────────────────────────────────────────
    log_level: str = "INFO"
    log_json: bool = True

    @property
    def allowed_origins_list(self) -> list[str]:
        return [o.strip() for o in self.allowed_origins.split(",") if o.strip()]


# 模組 import 時 freeze 一次,避免每個 request 重讀 env
settings = Settings()
