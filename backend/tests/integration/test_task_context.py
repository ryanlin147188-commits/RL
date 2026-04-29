"""RFC-9: ``app.db.sync_session.task_context`` provides Celery-side parity
with the FastAPI request path -- a sync session + tenant ContextVar so the
ORM ``before_flush`` listener auto-stamps ``organization_id`` on inserts.

Run via the testcontainers Postgres -- the sync engine inside
``sync_session`` reads the same DSN env vars the conftest already exports.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.db.sync_session import SessionLocal, task_context
from app.models import Defect

pytestmark = pytest.mark.integration


def test_task_context_stamps_org_id_on_insert(org_a) -> None:
    """A new TenantScoped row created inside ``task_context`` inherits org_id."""
    with task_context(org_id=org_a.org_id, username="celery-bot") as db:
        defect = Defect(
            project_id=org_a.project_id,
            code="BUG-T001",
            title="created from a Celery task",
        )
        db.add(defect)
        db.flush()
        defect_id = defect.id

    # Verify outside the task_context block — auto-commit on clean exit.
    with SessionLocal() as db:
        row = db.get(Defect, defect_id)
        assert row is not None
        assert row.organization_id == org_a.org_id


def test_task_context_rolls_back_on_exception(org_a) -> None:
    """Any exception inside the block must roll back the session."""
    class _SimulatedFailure(RuntimeError):
        pass

    with pytest.raises(_SimulatedFailure):
        with task_context(org_id=org_a.org_id) as db:
            db.add(
                Defect(
                    project_id=org_a.project_id,
                    code="BUG-T002",
                    title="should not commit",
                )
            )
            db.flush()
            raise _SimulatedFailure("kaboom")

    with SessionLocal() as db:
        rows = db.execute(
            select(Defect).where(Defect.code == "BUG-T002")
        ).scalars().all()
        assert rows == []


def test_task_context_resets_org_var_on_exit(org_a) -> None:
    """The ContextVar must NOT leak between tasks running on the same worker."""
    from app.auth.context import current_org_id

    assert current_org_id.get() is None

    with task_context(org_id=org_a.org_id):
        assert current_org_id.get() == org_a.org_id

    assert current_org_id.get() is None
