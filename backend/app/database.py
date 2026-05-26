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
    """應用程式啟動時同步 schema。

    預設由 Alembic 作為唯一 schema truth；只有設定 ALEMBIC_MANAGED=false 時才
    退回 legacy ``create_all`` escape hatch。
    """
    # 確保所有 model 都已被 import，才能讓 metadata 認識它們
    from app.models import (  # noqa: F401
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
        sprint_link,
        step_screenshot_baseline,
        test_round,
        testcase_content,
        todo_link,
        tree_node,
    )

    if _alembic_managed():
        # alembic 走同步 driver,在 thread pool 跑避免阻塞 event loop
        import asyncio
        await asyncio.to_thread(_run_alembic_upgrade_head)
    else:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)


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
