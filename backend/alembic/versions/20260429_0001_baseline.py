"""baseline — create all tables from current SQLAlchemy metadata

Revision ID: 0001_baseline
Revises:
Create Date: 2026-04-29

For a fresh database, this revision creates every table currently declared in
``app.models``. For an existing database that was previously bootstrapped via
``init_db()`` (i.e. ``Base.metadata.create_all`` + lightweight ALTER scripts),
operators should run::

    alembic stamp 0001_baseline

once to mark the DB as already at this baseline without re-creating tables.

All future schema changes must be added as new Alembic revisions on top of
this baseline.
"""
from typing import Sequence, Union

from alembic import op

from app.models import Base
import app.models  # noqa: F401 — register every model on Base.metadata

revision: str = "0001_baseline"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind)


def downgrade() -> None:
    bind = op.get_bind()
    Base.metadata.drop_all(bind=bind)
