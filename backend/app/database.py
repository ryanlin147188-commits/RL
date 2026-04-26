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
        execution_report,
        execution_step_log,
        project,
        project_device,
        project_env_var,
        recording,
        schedule,
        step_screenshot_baseline,
        test_round,
        testcase_content,
        tree_node,
    )

    from sqlalchemy import text

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # ── Lightweight in-place migrations ─────────────────────────────
        # PostgreSQL 支援 ADD COLUMN IF NOT EXISTS，可以在每次啟動安全執行。
        for stmt in (
            "ALTER TABLE projects ADD COLUMN IF NOT EXISTS description TEXT",
            "ALTER TABLE projects ADD COLUMN IF NOT EXISTS owner VARCHAR(100)",
            "ALTER TABLE projects ADD COLUMN IF NOT EXISTS status VARCHAR(40)",
            "ALTER TABLE projects ADD COLUMN IF NOT EXISTS start_date VARCHAR(20)",
            "ALTER TABLE projects ADD COLUMN IF NOT EXISTS target_date VARCHAR(20)",
            "ALTER TABLE projects ADD COLUMN IF NOT EXISTS tags VARCHAR(300)",
            "ALTER TABLE defects ADD COLUMN IF NOT EXISTS attachments_json JSON",
        ):
            try:
                await conn.execute(text(stmt))
            except Exception:
                # 若 defects 表不存在（首次冷啟動），忽略；create_all 之後會在下次啟動補上
                pass


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
