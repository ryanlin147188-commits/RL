"""Synchronous Session factory for Celery tasks.

Why a separate module?
- FastAPI handlers run on asyncpg via ``app.database.engine`` /
  ``AsyncSessionLocal``. Celery prefork workers each get their own event
  loop per task; sharing the FastAPI async engine across them produces
  ``Future attached to a different loop`` errors and leaks connections.
- Hence the rule: routers import from ``app.database``, tasks import from
  here. The two engines never see each other.

Usage in a Celery task::

    from app.db.sync_session import task_context

    @celery_app.task
    def run_tests(task_id, report_id, testcase_ids, *, org_id=None):
        with task_context(org_id=org_id) as db:
            report = db.get(ExecutionReport, report_id)
            ...

``task_context`` does three things:
1. Opens a fresh ``Session`` from the module-level ``SessionLocal``.
2. Sets the per-task tenant context (``current_org_id`` etc) so the ORM
   ``before_flush`` listener can auto-stamp ``organization_id`` on new
   :class:`TenantScoped` rows -- same mechanism that scopes the request path.
3. Commits on clean exit, rolls back on exception, always closes.

Engine lifecycle: the module-level engine is created lazily on the first
use inside each worker process. That makes it fork-safe under Celery's
default prefork pool.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator, Optional

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.auth.context import (
    current_is_superuser,
    current_org_id,
    current_username,
)
from app.config import settings


_engine: Optional[Engine] = None
_SessionFactory: Optional[sessionmaker] = None


def _get_session_factory() -> sessionmaker:
    """Build the engine + sessionmaker lazily on first call (fork-safe)."""
    global _engine, _SessionFactory
    if _SessionFactory is None:
        _engine = create_engine(
            settings.SYNC_DATABASE_URL,
            pool_pre_ping=True,
            pool_recycle=3600,
        )
        _SessionFactory = sessionmaker(
            bind=_engine, autoflush=False, autocommit=False, expire_on_commit=False
        )
    return _SessionFactory


def SessionLocal() -> Session:
    """Convenience accessor mirroring the FastAPI side's ``AsyncSessionLocal``."""
    return _get_session_factory()()


@contextmanager
def task_context(
    *,
    org_id: Optional[str] = None,
    username: Optional[str] = None,
    is_superuser: bool = False,
) -> Iterator[Session]:
    """Open a sync DB session bound to a tenant context for the duration of
    the block.

    The caller must pass ``org_id`` (sourced from whatever queued the task --
    typically a column on the row being processed, e.g.
    ``ExecutionReport.organization_id``). Without it the ORM auto-stamp
    listener has no value to copy onto new rows, so any inserts inside the
    block would land with ``organization_id = NULL``.

    Commits on clean exit; rolls back on exception; always closes.
    """
    org_token = current_org_id.set(org_id)
    user_token = current_username.set(username)
    su_token = current_is_superuser.set(is_superuser)

    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:  # noqa: BLE001
        session.rollback()
        raise
    finally:
        session.close()
        current_is_superuser.reset(su_token)
        current_username.reset(user_token)
        current_org_id.reset(org_token)
