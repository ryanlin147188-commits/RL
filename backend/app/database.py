import os
from pathlib import Path

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.auth.tenant import install_tenant_autostamp
from app.config import settings
from app.models.base import Base

# ORM event hook: auto-fill organization_id on new TenantScoped rows from the
# request ContextVar. Registered once at module import (idempotent).
install_tenant_autostamp()

# ORM event hook: every newly-created TreeNode(testcase) / TestDocument /
# RecordingSession / ExecutionReport auto-spawns a pending ReviewRecord so
# admins see it in the Review Center without anyone calling the submit API.
# Imported lazily here to avoid circular imports during model bootstrap.
def _install_review_autocreate() -> None:
    from app.services.review_service import install_review_autocreate
    install_review_autocreate()
_install_review_autocreate()

engine = create_async_engine(
    settings.DATABASE_URL,
    echo=settings.DEBUG,
    pool_pre_ping=True,
    pool_recycle=3600,
)


def _alembic_managed() -> bool:
    """Whether Alembic is the source of truth for schema (default True).

    Set ALEMBIC_MANAGED=false to fall back to the legacy create_all path —
    only useful as an escape hatch if Alembic is broken in a deployment.
    """
    return os.environ.get("ALEMBIC_MANAGED", "true").strip().lower() not in ("false", "0", "no")


def _run_alembic_upgrade_head() -> None:
    """Run ``alembic upgrade head`` programmatically against SYNC_DATABASE_URL.

    For a fresh DB, baseline revision calls ``Base.metadata.create_all`` and
    every table is born. For an existing DB previously bootstrapped via the
    legacy create_all path, baseline ``create_all`` is a no-op (checkfirst=True)
    and Alembic just stamps the version row — making the upgrade idempotent
    in both directions.
    """
    from alembic import command
    from alembic.config import Config

    backend_root = Path(__file__).resolve().parent.parent
    cfg = Config(str(backend_root / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend_root / "alembic"))
    cfg.set_main_option("sqlalchemy.url", settings.SYNC_DATABASE_URL)
    command.upgrade(cfg, "head")

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
        group,
        mock_endpoint,
        org_invite,
        test_version,
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

    # 1) Schema:Alembic 為事實來源(baseline 內含 create_all,對既有 DB 為 no-op + stamp)
    if _alembic_managed():
        # alembic 走同步 driver,在 thread pool 跑避免阻塞 event loop
        import asyncio
        await asyncio.to_thread(_run_alembic_upgrade_head)
    else:
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
        # User 加 avatar_url(本來只有文字頭像)
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS avatar_url VARCHAR(500)",
        # AI Token Config:加 reasoning_effort(low/medium/high,o1/o3 系列才會用)
        "ALTER TABLE ai_token_configs ADD COLUMN IF NOT EXISTS reasoning_effort VARCHAR(10)",
        # provider 從 enum 改為 varchar 以支援自由輸入(GROQ / DeepSeek / Together / ...)
        "ALTER TABLE ai_token_configs ALTER COLUMN provider TYPE VARCHAR(40)",
        "DROP TYPE IF EXISTS aiprovider CASCADE",
        # Todo 任務指派 V1:assignee_type / assigned_by / assigned_at
        # assignee 仍是 String;assignee_type 區分 user(值=username)還是 group(值=group_id)
        "ALTER TABLE todo_items ADD COLUMN IF NOT EXISTS assignee_type VARCHAR(10) NOT NULL DEFAULT 'user'",
        "ALTER TABLE todo_items ADD COLUMN IF NOT EXISTS assigned_by VARCHAR(80)",
        "ALTER TABLE todo_items ADD COLUMN IF NOT EXISTS assigned_at TIMESTAMP",
        # Groups (團隊群組;create_all 會建表,index 補保險)
        "CREATE INDEX IF NOT EXISTS ix_groups_org ON groups (organization_id)",
        "CREATE INDEX IF NOT EXISTS ix_groups_parent ON groups (parent_id)",
        # Q4 自動歸屬:organizations 加 email_domains
        "ALTER TABLE organizations ADD COLUMN IF NOT EXISTS email_domains VARCHAR(500)",
        # Q4 邀請碼(create_all 會建表,index 補保險;token 已是 unique)
        "CREATE INDEX IF NOT EXISTS ix_org_invites_org ON org_invites (organization_id)",
        # TestVersion(create_all 會建 test_versions 表)
        "CREATE INDEX IF NOT EXISTS ix_test_versions_org ON test_versions (organization_id)",
        "CREATE INDEX IF NOT EXISTS ix_test_versions_project ON test_versions (project_id)",
        # 三表反向 FK:nullable + ON DELETE SET NULL
        "ALTER TABLE execution_reports ADD COLUMN IF NOT EXISTS test_version_id VARCHAR(36)",
        "ALTER TABLE defects ADD COLUMN IF NOT EXISTS test_version_id VARCHAR(36)",
        "ALTER TABLE test_rounds ADD COLUMN IF NOT EXISTS test_version_id VARCHAR(36)",
        "CREATE INDEX IF NOT EXISTS ix_execution_reports_tv ON execution_reports (test_version_id)",
        "CREATE INDEX IF NOT EXISTS ix_defects_tv ON defects (test_version_id)",
        "CREATE INDEX IF NOT EXISTS ix_test_rounds_tv ON test_rounds (test_version_id)",
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
