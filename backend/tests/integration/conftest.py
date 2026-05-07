"""Integration-only fixtures: real Postgres + AsyncClient + org factories.

Layered so that:
  * session-scoped Postgres testcontainer
  * Alembic upgrade head once per session (proves the baseline migration works)
  * function-scoped TRUNCATE so tests do not leak rows into each other
  * AsyncClient against the live ASGI app
  * helpers to mint two isolated organisations + admin tokens

**Why eager testcontainer startup at module load time?**
有些 test module(test_auth_flow.py / test_assignments.py …)在最頂端
``from app.database import AsyncSessionLocal`` 直接 import。pytest collection
階段就會載入這些模組;那一刻 ``app.config.settings = Settings()`` 也跟著被
初始化,讀到的是 OS env 預設值(``localhost:5432``)而非 testcontainer 實際
port,於是 ``app.database.engine`` 凍在錯誤的 URL 上,晚一步在 fixture 才
overwrite ``DB_*`` 已經來不及。

解法:在 conftest 模組載入(也就是 collection 開始之前)就先把 Postgres /
Valkey 兩顆 container 啟好,把實際 host:port 寫進 ``os.environ``,再讓
pytest 開始 collect。後面的 ``postgres_container`` / ``valkey_container``
fixture 仍存在,只是改成 yield 已經啟好的 URL,不再二次啟動。
"""
from __future__ import annotations

import atexit
import os
import uuid
from pathlib import Path
from typing import AsyncIterator, Iterator

import pytest


# ── Eager testcontainer bootstrap(module-level,collection 之前)──────────
def _bootstrap_eager_containers() -> None:
    try:
        from testcontainers.postgres import PostgresContainer
        from testcontainers.redis import RedisContainer
    except ImportError:
        # 沒裝 testcontainers 就不啟,讓後面的 fixture 走 skip 路徑
        return
    try:
        pg = PostgresContainer("postgres:16-alpine").start()
        vk = RedisContainer("valkey/valkey:8-alpine").start()
    except Exception:
        # Docker 沒開、或 image pull 失敗,讓後面的 fixture 自己處理 skip
        return

    os.environ["DB_HOST"] = pg.get_container_host_ip()
    os.environ["DB_PORT"] = str(pg.get_exposed_port(5432))
    os.environ["DB_USER"] = pg.username
    os.environ["DB_PASSWORD"] = pg.password
    os.environ["DB_NAME"] = pg.dbname
    os.environ["REDIS_URL"] = (
        f"redis://{vk.get_container_host_ip()}:{vk.get_exposed_port(6379)}/0"
    )

    # 收尾:程式結束時(包含 pytest 全部跑完)才停容器
    @atexit.register
    def _stop_eager_containers() -> None:  # noqa: C901  — best-effort
        try:
            pg.stop()
        except Exception:
            pass
        try:
            vk.stop()
        except Exception:
            pass

    # Stash 給後面 fixture 取用
    globals()["_EAGER_PG"] = pg
    globals()["_EAGER_VK"] = vk


_bootstrap_eager_containers()


# ── 0) Valkey/Redis testcontainer (session-scoped) ────────────────────────
# Required for RFC-6 token revocation tests. The revocation module fails
# open when REDIS_URL is unreachable, so other integration tests still pass
# without a live cache — but to *verify* a token is actually revoked we need
# Valkey running. We point REDIS_URL at the container before the app is
# imported so any module-level redis client picks it up.

@pytest.fixture(scope="session")
def valkey_container() -> Iterator[str]:
    eager = globals().get("_EAGER_VK")
    if eager is not None:
        yield os.environ["REDIS_URL"]
        return
    try:
        from testcontainers.redis import RedisContainer
    except ImportError:
        pytest.skip("testcontainers[redis] not installed")

    try:
        with RedisContainer("valkey/valkey:8-alpine") as vk:
            host = vk.get_container_host_ip()
            port = vk.get_exposed_port(6379)
            url = f"redis://{host}:{port}/0"
            os.environ["REDIS_URL"] = url
            yield url
    except Exception as exc:
        pytest.skip(f"Valkey container unavailable: {exc}")


# ── 1) Postgres testcontainer (session-scoped) ────────────────────────────

@pytest.fixture(scope="session")
def postgres_container() -> Iterator[str]:
    """Yield asyncpg DSN.

    優先使用 module-level 在 collection 之前就啟好的容器(避免 app.database
    在 module-level import 被凍結到 default URL);沒成功啟到才走原本的
    testcontainers context manager 兜底。
    """
    eager = globals().get("_EAGER_PG")
    if eager is not None:
        host = eager.get_container_host_ip()
        port = eager.get_exposed_port(5432)
        url = (
            f"postgresql+asyncpg://{eager.username}:{eager.password}"
            f"@{host}:{port}/{eager.dbname}"
        )
        yield url
        return

    try:
        from testcontainers.postgres import PostgresContainer
    except ImportError:
        pytest.skip("testcontainers not installed")

    try:
        with PostgresContainer("postgres:16-alpine") as pg:
            host = pg.get_container_host_ip()
            port = pg.get_exposed_port(5432)
            user = pg.username
            password = pg.password
            db = pg.dbname

            os.environ["DB_HOST"] = host
            os.environ["DB_PORT"] = str(port)
            os.environ["DB_USER"] = user
            os.environ["DB_PASSWORD"] = password
            os.environ["DB_NAME"] = db

            yield f"postgresql+asyncpg://{user}:{password}@{host}:{port}/{db}"
    except Exception as exc:  # docker not available
        pytest.skip(f"Postgres container unavailable: {exc}")


# ── 2) Alembic upgrade once per session ───────────────────────────────────

@pytest.fixture(scope="session")
def _migrated_db(postgres_container: str, valkey_container: str) -> str:
    """Apply ``alembic upgrade head`` against the testcontainer DB.

    Also depends on ``valkey_container`` so REDIS_URL is set before the app
    package is imported by any later fixture (revocation module reads it).
    """
    from alembic import command
    from alembic.config import Config

    backend_root = Path(__file__).resolve().parents[2]
    cfg = Config(str(backend_root / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend_root / "alembic"))
    sync_url = (
        f"postgresql+psycopg://{os.environ['DB_USER']}:{os.environ['DB_PASSWORD']}"
        f"@{os.environ['DB_HOST']}:{os.environ['DB_PORT']}/{os.environ['DB_NAME']}"
    )
    cfg.set_main_option("sqlalchemy.url", sync_url)
    command.upgrade(cfg, "head")
    return postgres_container


# ── 2.5) 把 import-time 建好的 AsyncEngine 重建為 NullPool,綁到 session loop
# `app.database.engine` 在 module import(collection 階段)就被 create_async_engine
# 出來,connection pool 內部抓到的是 asyncio default loop;但 pytest-asyncio 啟動
# 時會新開一個 session loop,兩者不同,asyncpg 會丟「Future attached to a
# different loop」。
#
# 光 dispose() 不夠 — engine + sessionmaker 內部仍綁舊 loop。改成:用 NullPool
# 建一顆新的 AsyncEngine(每次連線都即時新建,綁當前 loop),覆蓋
# app.database.engine / AsyncSessionLocal,並把已經在 module level
# `from app.database import AsyncSessionLocal` 的 test module 的 binding 一起
# 換成新的。

@pytest.fixture(scope="session", autouse=True)
async def _rebind_engine_to_session_loop(_migrated_db: str) -> AsyncIterator[None]:
    import sys

    from sqlalchemy.ext.asyncio import (
        AsyncSession,
        async_sessionmaker,
        create_async_engine,
    )
    from sqlalchemy.pool import NullPool

    import app.database as db_mod
    from app.config import Settings

    try:
        await db_mod.engine.dispose()
    except Exception:
        pass

    new_engine = create_async_engine(
        Settings().DATABASE_URL,
        poolclass=NullPool,
        pool_pre_ping=False,
    )
    new_session_factory = async_sessionmaker(
        new_engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
        autocommit=False,
    )
    db_mod.engine = new_engine
    db_mod.AsyncSessionLocal = new_session_factory

    # 同步換掉測試模組(module level import 早就抓到舊物件)的 binding
    for mod_name in list(sys.modules.keys()):
        if not mod_name.startswith("tests.integration."):
            continue
        mod = sys.modules.get(mod_name)
        if mod is None:
            continue
        if getattr(mod, "AsyncSessionLocal", None) is not None:
            mod.AsyncSessionLocal = new_session_factory
        if getattr(mod, "engine", None) is not None:
            mod.engine = new_engine

    yield

    await new_engine.dispose()


# ── 3) Truncate between tests so state does not leak ──────────────────────

@pytest.fixture(autouse=True)
async def _truncate_between_tests(_migrated_db: str) -> AsyncIterator[None]:
    """Wipe all rows but keep the schema. Cheap (~10ms) compared to recreating."""
    yield
    from sqlalchemy import text
    from app.database import engine

    async with engine.begin() as conn:
        result = await conn.execute(
            text(
                "SELECT tablename FROM pg_tables "
                "WHERE schemaname='public' AND tablename != 'alembic_version'"
            )
        )
        tables = [row[0] for row in result.fetchall()]
        if tables:
            joined = ", ".join(f'"{t}"' for t in tables)
            await conn.execute(text(f"TRUNCATE {joined} RESTART IDENTITY CASCADE"))


# ── 4) AsyncClient against the FastAPI app ────────────────────────────────

@pytest.fixture
async def client(_migrated_db: str) -> AsyncIterator:
    """Yield an httpx.AsyncClient bound to the live FastAPI app.

    The app is imported lazily so env vars set above take effect. Each test
    seeds default roles + default org via the lifespan helpers.
    """
    from httpx import ASGITransport, AsyncClient
    from app.main import app, _seed_default_roles, _seed_default_org_and_backfill

    await _seed_default_roles()
    await _seed_default_org_and_backfill()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# ── 5) Helpers: build isolated orgs with admin tokens ─────────────────────

class OrgFixture:
    """A single tenant with one admin user, one project, and a bearer token."""

    def __init__(self, *, org_id: str, username: str, token: str, project_id: str):
        self.org_id = org_id
        self.username = username
        self.token = token
        self.project_id = project_id
        self.headers = {"Authorization": f"Bearer {token}"}


async def _make_org(slug_prefix: str) -> OrgFixture:
    """Create an Organization, an admin User in it, and one Project.

    也建一筆 ``ProjectMember`` row(status='active'),否則 admin 走
    ``ensure_project_in_scope`` 會被擋(404 Not Found)— 這個 helper 要讓
    回來的 admin 可以順利讀寫自己的 project。
    """
    from sqlalchemy import select
    from app.auth.security import create_access_token, hash_password
    from app.database import AsyncSessionLocal
    from app.models import Organization, Project, ProjectMember, Role, User

    suffix = uuid.uuid4().hex[:8]
    slug = f"{slug_prefix}-{suffix}"
    username = f"{slug_prefix}-admin-{suffix}"

    async with AsyncSessionLocal() as session:
        org = Organization(slug=slug, name=slug, plan="free")
        session.add(org)
        await session.flush()

        admin_role = (
            await session.execute(select(Role).where(Role.name == "Admin"))
        ).scalar_one_or_none()

        user = User(
            username=username,
            display_name=username,
            email=f"{username}@test.local",
            password_hash=hash_password("test-password-123"),
            role_id=admin_role.id if admin_role else None,
            organization_id=org.id,
            is_superuser=False,
            is_active=True,
        )
        session.add(user)
        await session.flush()

        project = Project(
            id=str(uuid.uuid4()),
            name=f"{slug}-project",
            organization_id=org.id,
        )
        session.add(project)
        await session.flush()

        # 沒有 ProjectMember active row,ensure_project_in_scope 會回 404
        session.add(
            ProjectMember(
                id=str(uuid.uuid4()),
                project_id=project.id,
                username=username,
                role_id=admin_role.id if admin_role else None,
                status="active",
            )
        )
        await session.commit()

        org_id = org.id
        project_id = project.id

    token = create_access_token(
        username,
        extra={"org_id": org_id, "is_superuser": False},
    )
    return OrgFixture(org_id=org_id, username=username, token=token, project_id=project_id)


@pytest.fixture
async def org_a(client) -> OrgFixture:
    return await _make_org("orga")


@pytest.fixture
async def org_b(client) -> OrgFixture:
    return await _make_org("orgb")


# ── 7) RBAC role factory: same org, different role ────────────────────────

class RoleFixture:
    """A user inside an existing org with a specific role (Admin / QA / Viewer)."""

    def __init__(self, *, role: str, username: str, token: str, org: OrgFixture):
        self.role = role
        self.username = username
        self.token = token
        self.org = org
        self.headers = {"Authorization": f"Bearer {token}"}


async def _make_user_in_org(org: OrgFixture, role_name: str) -> RoleFixture:
    """Mint a non-superuser bound to ``role_name`` ('Admin'|'QA'|'Viewer')."""
    import uuid as _uuid

    from sqlalchemy import select
    from app.auth.security import create_access_token, hash_password
    from app.database import AsyncSessionLocal
    from app.models import Role, User

    suffix = _uuid.uuid4().hex[:8]
    username = f"{role_name.lower()}-{suffix}"

    async with AsyncSessionLocal() as session:
        role = (
            await session.execute(select(Role).where(Role.name == role_name))
        ).scalar_one()
        user = User(
            username=username,
            display_name=username,
            email=f"{username}@test.local",
            password_hash=hash_password("test-password-123"),
            role_id=role.id,
            organization_id=org.org_id,
            is_superuser=False,
            is_active=True,
        )
        session.add(user)
        await session.commit()

    token = create_access_token(
        username,
        extra={"org_id": org.org_id, "is_superuser": False},
    )
    return RoleFixture(role=role_name, username=username, token=token, org=org)


@pytest.fixture
async def viewer_in_a(org_a) -> RoleFixture:
    return await _make_user_in_org(org_a, "Viewer")


@pytest.fixture
async def qa_in_a(org_a) -> RoleFixture:
    return await _make_user_in_org(org_a, "QA")


@pytest.fixture
async def admin_in_a(org_a) -> RoleFixture:
    return await _make_user_in_org(org_a, "Admin")
