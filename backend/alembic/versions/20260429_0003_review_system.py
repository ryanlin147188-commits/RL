"""review_system -- review_records + review_history tables

Revision ID: 0003_review_system
Revises: 0002_tenant_scope
Create Date: 2026-04-29

Adds the generic review/approval workflow (RFC-Review-1):

  * review_records   one row per (entity_type, entity_id, organization_id)
                     captures the current pending/approved/rejected state.
  * review_history   append-only audit trail of every state transition,
                     including reason on reject/revert.

Both tables are TenantScoped and indexed for the hot lookup path
"is X approved?" used by the lock enforcement helper.

Implementation note: uses ``Base.metadata.create_all(checkfirst=True)``
restricted to the two new tables. This is idempotent for both deployment
flows:

  * Fresh install: baseline (0001) already ran ``Base.metadata.create_all``
    over the full current metadata, so review_records / review_history are
    already present. 0003 sees existing tables (and existing ENUM types)
    and skips them.
  * Old install (was at 0002 before the review feature shipped): tables
    do not exist yet; 0003 creates them along with their ENUM types via
    SQLAlchemy's normal type-creation flow (checkfirst guards too).
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0003_review_system"
down_revision: Union[str, None] = "0002_tenant_scope"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Lazy import: avoid pulling all models at module-import time so
    # alembic offline mode (--sql) can still render this migration.
    from app.models import Base, ReviewHistory, ReviewRecord

    bind = op.get_bind()
    Base.metadata.create_all(
        bind=bind,
        tables=[ReviewRecord.__table__, ReviewHistory.__table__],
        checkfirst=True,
    )


def downgrade() -> None:
    from app.models import Base, ReviewHistory, ReviewRecord

    bind = op.get_bind()
    Base.metadata.drop_all(
        bind=bind,
        tables=[ReviewHistory.__table__, ReviewRecord.__table__],
        checkfirst=True,
    )
    # Drop the ENUM types last (no-op if other tables still reference them).
    op.execute("DROP TYPE IF EXISTS review_action")
    op.execute("DROP TYPE IF EXISTS review_status")
    op.execute("DROP TYPE IF EXISTS reviewable_entity_type")
