from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import settings
from app.models.base import Base

engine = create_async_engine(
    settings.DATABASE_URL,
    echo=settings.DEBUG,
    pool_pre_ping=True,
    pool_recycle=3600,
)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
    autocommit=False,
)


async def init_db() -> None:
    """應用程式啟動時，若資料表不存在則自動建立，並補上新增欄位（idempotent）。"""
    # 確保所有 model 都已被 import，才能讓 metadata 認識它們
    from app.models import (  # noqa: F401
        ai_conversation,
        db_config,
        execution_report,
        execution_step_log,
        mock_endpoint,
        project,
        project_device,
        project_env_var,
        recording,
        schedule,
        step_screenshot_baseline,
        test_round,
        testcase_content,
        todo_link,
        tree_node,
    )

    from sqlalchemy import text

    # 1) create_all 在自己的 transaction;失敗就讓服務啟動失敗,看到錯誤
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # 2) Lightweight migrations:每條 statement 在獨立 transaction 執行,
    #    避免一條失敗讓整批 rollback。PostgreSQL 在 transaction abort 後
    #    會拒絕後續所有 statement,連 try/except 也救不回來,所以一定要分開。
    async def _run_safe(stmt: str) -> None:
        try:
            async with engine.begin() as c:
                await c.execute(text(stmt))
        except Exception:
            pass

    migration_stmts = (
        "ALTER TABLE projects ADD COLUMN IF NOT EXISTS description TEXT",
        "ALTER TABLE projects ADD COLUMN IF NOT EXISTS owner VARCHAR(100)",
        "ALTER TABLE projects ADD COLUMN IF NOT EXISTS status VARCHAR(40)",
        "ALTER TABLE projects ADD COLUMN IF NOT EXISTS start_date VARCHAR(20)",
        "ALTER TABLE projects ADD COLUMN IF NOT EXISTS target_date VARCHAR(20)",
        "ALTER TABLE projects ADD COLUMN IF NOT EXISTS tags VARCHAR(300)",
        "ALTER TABLE defects ADD COLUMN IF NOT EXISTS attachments_json JSON",
        # Multi-tenancy
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS organization_id VARCHAR(36)",
        "ALTER TABLE projects ADD COLUMN IF NOT EXISTS organization_id VARCHAR(36)",
        "ALTER TABLE roles ADD COLUMN IF NOT EXISTS organization_id VARCHAR(36)",
        "ALTER TABLE email_configs ADD COLUMN IF NOT EXISTS organization_id VARCHAR(36)",
        "ALTER TABLE ai_token_configs ADD COLUMN IF NOT EXISTS organization_id VARCHAR(36)",
        "ALTER TABLE todo_items ADD COLUMN IF NOT EXISTS organization_id VARCHAR(36)",
        "ALTER TABLE roles DROP CONSTRAINT IF EXISTS roles_name_key",
        "CREATE INDEX IF NOT EXISTS ix_users_org ON users (organization_id)",
        "CREATE INDEX IF NOT EXISTS ix_projects_org ON projects (organization_id)",
        "CREATE INDEX IF NOT EXISTS ix_roles_org ON roles (organization_id)",
        "CREATE INDEX IF NOT EXISTS ix_email_configs_org ON email_configs (organization_id)",
        "CREATE INDEX IF NOT EXISTS ix_ai_token_configs_org ON ai_token_configs (organization_id)",
        "CREATE INDEX IF NOT EXISTS ix_todo_items_org ON todo_items (organization_id)",
        "CREATE INDEX IF NOT EXISTS ix_oidc_providers_org ON oidc_providers (organization_id)",
        # Backlog hierarchy on todo_items
        "ALTER TABLE todo_items ADD COLUMN IF NOT EXISTS item_type VARCHAR(20) NOT NULL DEFAULT 'Task'",
        "ALTER TABLE todo_items ADD COLUMN IF NOT EXISTS parent_id VARCHAR(36)",
        "ALTER TABLE todo_items ADD COLUMN IF NOT EXISTS sprint_label VARCHAR(80)",
        "CREATE INDEX IF NOT EXISTS ix_todo_items_parent ON todo_items (parent_id)",
        "CREATE INDEX IF NOT EXISTS ix_todo_items_sprint ON todo_items (sprint_label)",
        # Mock + DB connection persistence (取代 localStorage)
        "CREATE INDEX IF NOT EXISTS ix_mock_endpoints_org ON mock_endpoints (organization_id)",
        "CREATE INDEX IF NOT EXISTS ix_mock_endpoints_project ON mock_endpoints (project_id)",
        "CREATE INDEX IF NOT EXISTS ix_db_configs_org ON db_configs (organization_id)",
        "CREATE INDEX IF NOT EXISTS ix_db_configs_project ON db_configs (project_id)",
        # Backlog 階層調整(Epic/Story → Feature)+ todo_links 連結表
        # 注意:SQLAlchemy Enum() 對 varchar 欄位存的是 enum 「名稱」(大寫,例 EPIC),
        # 不是 .value(Title Case)。所以 WHERE 用大寫字串比對。
        # 0) 把舊的 PostgreSQL native enum type todoitemtype 拿掉(欄位本身是 varchar,
        #    這個 type 是早期 create_all 留下的孤兒物件;改用 native_enum=False 後不再需要)
        "DROP TYPE IF EXISTS todoitemtype CASCADE",
        # 1) Epic / EPIC 直接改名 Feature / FEATURE(階層位置不變)
        "UPDATE todo_items SET item_type='FEATURE' WHERE item_type IN ('EPIC','Epic')",
        # 2) Story / STORY 升頂為 FEATURE(parent_id 設 NULL),
        #    其 Task/Bug/Spike 子節點仍指向同一列(現在 type=FEATURE),
        #    新規則允許 Task/Bug/Spike → Feature,所以父子鏈條保持合法
        "UPDATE todo_items SET item_type='FEATURE', parent_id=NULL WHERE item_type IN ('STORY','Story')",
        # 3) todo_links indexes(create_all 會建表,index 補保險)
        "CREATE INDEX IF NOT EXISTS ix_todo_links_todo ON todo_links (todo_id)",
        "CREATE INDEX IF NOT EXISTS ix_todo_links_target ON todo_links (target_type, target_id)",
        "CREATE INDEX IF NOT EXISTS ix_todo_links_org ON todo_links (organization_id)",
        # AI 對話表 indexes
        "CREATE INDEX IF NOT EXISTS ix_ai_conversations_owner ON ai_conversations (owner)",
        "CREATE INDEX IF NOT EXISTS ix_ai_conversations_org ON ai_conversations (organization_id)",
        "CREATE INDEX IF NOT EXISTS ix_ai_messages_conv ON ai_messages (conversation_id)",
    )
    for stmt in migration_stmts:
        await _run_safe(stmt)


async def get_db():
    """FastAPI 依賴注入：取得 AsyncSession。"""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
