"""Integration-only fixtures: real Postgres + AsyncClient + org factories.

Layered so that:
  * session-scoped Postgres testcontainer
  * Alembic upgrade head once per session (proves the baseline migration works)
  * function-scoped TRUNCATE so tests do not leak rows into each other
  * AsyncClient against the live ASGI app
  * helpers to mint two isolated organisations + admin tokens
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import AsyncIterator, Iterator

import pytest


# ── 0) Valkey/Redis testcontainer (session-scoped) ────────────────────────
# Required for RFC-6 token revocation tests. The revocation module fails
# open when REDIS_URL is unreachable, so other integration tests still pass
# without a live cache — but to *verify* a token is actually revoked we need
# Valkey running. We point REDIS_URL at the container before the app is
# imported so any module-level redis client picks it up.

@pytest.fixture(scope="session")
def valkey_container() -> Iterator[str]:
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
    """Boot a Postgres 16 container; yield an asyncpg DSN.

    Skips the whole integration session if Docker is unavailable so a quick
    ``pytest tests/unit`` still works on a contributor laptop without Docker.
    """
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
    """Create an Organization, an admin User in it, and one Project."""
    from sqlalchemy import select
    from app.auth.security import create_access_token, hash_password
    from app.database import AsyncSessionLocal
    from app.models import Organization, Project, Role, User

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
