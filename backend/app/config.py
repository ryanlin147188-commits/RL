from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


_FORBIDDEN_DEFAULTS = {"admin123", "changeme", "password", "secret"}


class Settings(BaseSettings):
    # 資料庫（PostgreSQL）— 預設 port 5432
    DB_HOST: str = "localhost"
    DB_PORT: int = 5432
    DB_USER: str = "admin"
    # 必填:啟動前環境變數須提供,且不可為已知弱值。
    DB_PASSWORD: str = Field(..., min_length=8)
    DB_NAME: str = "autotest_db"

    # 快取 / Celery broker（Valkey；wire-protocol 與 Redis 100% 相容）
    # URL scheme 沿用 redis:// 因為 redis-py / celery 都認得
    REDIS_URL: str = "redis://localhost:6379/0"

    # 截圖存放目錄
    PIC_FOLDER: str = "./PIC"
    BASE_URL: str = "http://localhost:8000"

    # 錄製腳本在「使用者本機」執行時，預設切換到的專案根目錄（用於 record/<sid> 相對路徑基準）
    # 預設 "." = 使用者當前工作目錄；需要固定到特定路徑時再用環境變數覆蓋
    # 跨平台：Windows 用 "C:/path/to/proj"、macOS/Linux 用 "/Users/you/proj" 或 "/home/you/proj"
    RECORDER_HOST_ROOT: str = "."

    # 物件儲存：s3(透過 SeaweedFS 提供 S3-compatible API)
    # SeaweedFS 預設 S3 port 8333；服務名稱 seaweedfs
    STORAGE_BACKEND: str = "s3"
    S3_ENDPOINT: str = "http://seaweedfs:8333"
    S3_ACCESS_KEY: str = "admin"
    # 必填:由 docker-compose 從 S3_ROOT_PASSWORD 映射而來,本機 dev 須手動設。
    S3_SECRET_KEY: str = Field(..., min_length=8)

    # 應用程式
    APP_HOST: str = "0.0.0.0"
    APP_PORT: int = 8000
    # 預設關閉:DEBUG=True 會在 stack trace 洩漏檔案路徑與 env 內容。
    # 開發時用 `AUTOTEST_DEBUG=True docker compose up -d --force-recreate backend`
    # 啟用,或在本機 .env 設 DEBUG=True。
    DEBUG: bool = False

    # v1.1.13:啟用後 backend 對 /api/* 強制要求合法 X-Gateway-Verified HMAC,
    # 缺少或驗章失敗一律 401(不再退回獨立 JWT 直驗)。
    # 生產環境應設為 True 並把 backend 8000 port 從對外網路移除,只讓 gateway
    # 內網存取,把信任邊界收斂到 gateway 一層。預設 False 以利舊部署平滑升級。
    BACKEND_TRUST_GATEWAY_ONLY: bool = False

    # ─── Docker 模式錄製 ─────────────────────────────────────────────
    # v1.1.9 起 recorder / recorder-api / mcp 三個 image 合併成一份
    # autotest-recorder,用 RECORDER_MODE env 切換 entrypoint(novnc /
    # mitmweb / mcp)。舊版的 RECORDER_API_IMAGE / MCP_IMAGE env 已移除,
    # pydantic Settings 的 ``extra="ignore"`` 確保 .env 殘留也不會炸。
    RECORDER_IMAGE: str = "autotest-recorder:1.1.1"
    # 啟動的容器加入 docker compose 的同一 network(讓 codegen 完成後 curl
    # 上傳能解析到 backend hostname);預設與 docker-compose.yml networks 一致
    RECORDER_NETWORK: str = "autotest_default"
    # 容器內 codegen 完成後 curl 上傳的目標(從容器內看);走 internal hostname
    RECORDER_INTERNAL_BASE_URL: str = "http://backend:8000"
    # 預設 idle 多久後自動回收 recorder 容器(分鐘)
    RECORDER_IDLE_TIMEOUT_MIN: int = 30

    @property
    def DATABASE_URL(self) -> str:
        """Async URL 供 FastAPI / SQLAlchemy asyncio 使用（PostgreSQL via asyncpg）"""
        return (
            f"postgresql+asyncpg://{self.DB_USER}:{self.DB_PASSWORD}"
            f"@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}"
        )

    @property
    def SYNC_DATABASE_URL(self) -> str:
        """Sync URL 供 Celery Worker 使用（PostgreSQL via psycopg v3）"""
        return (
            f"postgresql+psycopg://{self.DB_USER}:{self.DB_PASSWORD}"
            f"@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}"
        )

    @property
    def CELERY_BROKER_URL(self) -> str:
        return self.REDIS_URL

    @property
    def CELERY_RESULT_BACKEND(self) -> str:
        return self.REDIS_URL

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @field_validator("DB_PASSWORD", "S3_SECRET_KEY")
    @classmethod
    def _reject_known_weak_secret(cls, v: str, info) -> str:
        if v.strip().lower() in _FORBIDDEN_DEFAULTS:
            raise ValueError(
                f"{info.field_name} 不可使用已知弱密碼(admin123 / changeme / password / secret),"
                "請改用 `openssl rand -hex 24` 等隨機值"
            )
        return v

    @field_validator("BASE_URL")
    @classmethod
    def _base_url_https_in_prod(cls, v: str) -> str:
        """BASE_URL 在公網部署必須使用 HTTPS。
        OIDC redirect_uri 由此推導,純 HTTP 會讓授權碼可被 MitM 截取。
        localhost 與 RFC1918 私有網段(10/8、172.16/12、192.168/16)在純內網
        部署常見,風險可控,允許 http。"""
        from urllib.parse import urlparse

        from app.auth._network import is_private_or_localhost_host

        parsed = urlparse(v)
        if parsed.scheme not in {"http", "https"}:
            raise ValueError(f"BASE_URL 必須以 http:// 或 https:// 開頭,目前是 {v}")
        if parsed.scheme == "http" and not is_private_or_localhost_host(parsed.hostname or ""):
            raise ValueError(
                f"BASE_URL 在公網部署必須使用 HTTPS,目前是 {v}。"
                " 純 HTTP 在公網會讓 OIDC 授權碼被 MitM 截取(內網 IP 例外)。"
            )
        return v


settings = Settings()
