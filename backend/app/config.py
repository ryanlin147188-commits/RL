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
    # WEB:容器內透過此 image 跑 Xvfb + noVNC + Playwright codegen
    RECORDER_IMAGE: str = "autotest-recorder:1.1.0"
    # API:容器內跑 mitmweb(HTTP proxy + web UI)+ HAR addon
    RECORDER_API_IMAGE: str = "autotest-recorder-api:1.1.0"
    # MCP:Playwright MCP server,讓 LLM 透過 tool calling 操作 chromium
    MCP_IMAGE: str = "autotest-mcp:1.1.0"
    # 啟動的容器加入 docker compose 的同一 network(讓 codegen 完成後 curl
    # 上傳能解析到 backend hostname);預設與 docker-compose.yml networks 一致
    RECORDER_NETWORK: str = "autotest_default"
    # 容器內 codegen 完成後 curl 上傳的目標(從容器內看);走 internal hostname
    RECORDER_INTERNAL_BASE_URL: str = "http://backend:8000"
    # 預設 idle 多久後自動回收 recorder 容器(分鐘)
    RECORDER_IDLE_TIMEOUT_MIN: int = 30

    # ─── Hermes Agent sidecar ───────────────────────────────────────
    # 內網 service-name 解析,backend 在同 docker network。
    HERMES_BASE_URL: str = "http://hermes:7800"
    # 與 sidecar 共享的 secret(必填),由 bootstrap 自動產生寫入 .env
    SIDECAR_AUTH_TOKEN: str = ""
    # 同步 prompt 的整體 timeout — 與 sidecar 內 HERMES_RPC_TIMEOUT 對齊
    HERMES_TIMEOUT_SEC: int = 60
    # Streaming 等更慢的呼叫上限
    HERMES_STREAM_TIMEOUT_SEC: int = 300
    # Feature flag:false → router 全 503,sidecar 掛點時降級用
    HERMES_ENABLED: bool = True

    # ─── mem0 sidecar(語意記憶層)──────────────────────────────────
    # 內網 service-name 解析,backend 跟 mem0 在同 docker network
    MEM0_BASE_URL: str = "http://mem0:7900"
    # 與 mem0 sidecar 共享 secret(必填,bootstrap 自動產;與 hermes SIDECAR_AUTH_TOKEN
    # 邊界分離 — 兩個 sidecar 各自獨立 auth)
    MEM0_SIDECAR_AUTH_TOKEN: str = ""
    # 一般 add/list/delete 同步 timeout;search 走更短(在 client 內覆寫)
    MEM0_TIMEOUT_SEC: int = 5
    MEM0_SEARCH_TIMEOUT_SEC: int = 3
    # Feature flag:false → 整 mem0 路徑跳過(post-hook 不觸發、router 略過 503)
    MEM0_ENABLED: bool = True
    # Per-user-per-day fact extraction 上限(plan §4 速率保護);PR3 暫不啟用,
    # 留 env 給 PR4/PR5 接 valkey counter
    MEM0_FACT_EXTRACTION_RATE_PER_DAY: int = 200
    # Pre-hook(PR6):send_message 之前 search 過往記憶 → 注入 prompt 前綴。
    # plan 使用者選「v1 同時做 read + write」,所以預設 True;false 時可獨立關掉
    # 自動 RAG 但保留 post-hook 寫入。
    MEM0_PREHOOK_ENABLED: bool = True
    # Pre-hook search 召回上限(top-k);threshold 越低召回越多但雜訊也多
    MEM0_PREHOOK_TOP_K: int = 5
    MEM0_PREHOOK_THRESHOLD: float = 0.3
    # ── Hermes ↔ mem0 MCP tool(讓 Hermes ACP 子進程的 LLM 可以主動 invoke
    # `search_memory` tool,不再只靠 backend pre-hook 一次性注入)──────
    # Feature flag:False → routers/hermes.py 不把 mcpServers 帶給 hermes;
    # backend pre-hook + post-hook 仍正常運作(雙重安全網之一)
    MEM0_HERMES_TOOL_ENABLED: bool = True
    # Hermes ACP 子進程要連的 mem0 MCP endpoint(streamable HTTP transport)。
    # mem0_proxy.py app.mount("/mcp", ...) 後,FastMCP 內部把 path 再 +"/mcp",
    # 實際 tool call 進 /mcp/mcp。內網 service-name 解析(同 docker network)。
    MEM0_HERMES_TOOL_URL: str = "http://mem0:7900/mcp/mcp"

    # ── Platform MCP tool(讓 Hermes ACP LLM 直接呼叫平台 API:create_project /
    # list_projects / 等)。Backend mount /platform-mcp/mcp;auth 同樣走 SIDECAR_AUTH_TOKEN
    # + 多帶一層 X-Platform-User 識別呼叫者。
    PLATFORM_MCP_ENABLED: bool = True
    PLATFORM_MCP_URL: str = "http://backend:8000/platform-mcp/mcp"

    # ── Playwright MCP(per-user autotest-mcp 容器,讓助理真的能操作瀏覽器)──
    # True → ensure_user_workspace 時會 lazy-spin autotest-mcp container 並
    # 把它加進 Hermes 的 mcp_servers list;LLM 收到 browser_navigate / click /
    # snapshot 等 22 個 Playwright tool。第一次啟可能慢(image build),失敗
    # 不擋主流程 — 助理 fallback 到只用平台/記憶 tool。
    PLAYWRIGHT_MCP_HERMES_ENABLED: bool = True

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
