from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # 資料庫（PostgreSQL）— 預設 port 5432
    DB_HOST: str = "localhost"
    DB_PORT: int = 5432
    DB_USER: str = "admin"
    DB_PASSWORD: str = "admin123"
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
    S3_SECRET_KEY: str = "admin123"

    # 應用程式
    APP_HOST: str = "0.0.0.0"
    APP_PORT: int = 8000
    # 預設關閉:DEBUG=True 會在 stack trace 洩漏檔案路徑與 env 內容。
    # 開發時用 `AUTOTEST_DEBUG=True docker compose up -d --force-recreate backend`
    # 啟用,或在本機 .env 設 DEBUG=True。
    DEBUG: bool = False

    # ─── Docker 模式錄製(Phase 1) ────────────────────────────────────
    # 預設 tag 與 docker-compose.yml 的 ${AUTOTEST_TAG:-1.1.1} 對齊;升版時
    # 同步改這三個 + compose 檔的 AUTOTEST_TAG。可用 env var 覆蓋(RECORDER_IMAGE / 等)。
    # WEB:容器內透過此 image 跑 Xvfb + noVNC + Playwright codegen
    RECORDER_IMAGE: str = "autotest-recorder:1.1.1"
    # API:容器內跑 mitmweb(HTTP proxy + web UI)+ HAR addon
    RECORDER_API_IMAGE: str = "autotest-recorder-api:1.1.1"
    # MCP:Playwright MCP server,讓 LLM 透過 tool calling 操作 chromium
    MCP_IMAGE: str = "autotest-mcp:1.1.1"
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


settings = Settings()
