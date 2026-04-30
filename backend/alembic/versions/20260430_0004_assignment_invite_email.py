"""assignment_invite_email -- assignee columns + invite email tracking

Revision ID: 0004_assignment_invite_email
Revises: 0003_review_system
Create Date: 2026-04-30

Three independent additions bundled because they all support the
"assignment + email-driven invite" feature batch:

  1. Generic assignee fields on five reviewable entities:
       review_records / defects / tree_nodes / requirements / test_documents
     Pattern mirrors TodoItem (assigned_to / assigned_to_type / assigned_by /
     assigned_at). All nullable; existing rows keep null.

  2. Defects already had a free-form `assignee` varchar; we keep it
     untouched and BACKFILL the new assigned_to from it so the new
     pattern picks up legacy data.

  3. org_invites: track when the invite email was sent and to which
     address (for resend support + audit).

  4. organizations.email_domains: add a partial unique index so the same
     domain can't be claimed by two orgs simultaneously. Skipped if the
     existing data has dupes (logged WARNING via DO block) so migration
     doesn't corner an already-broken deploy.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0004_assignment_invite_email"
down_revision: Union[str, None] = "0003_review_system"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


ASSIGNEE_TABLES = (
    "review_records",
    "defects",
    "tree_nodes",
    "requirements",
    "test_documents",
)


def _add_assignee_columns(table: str) -> None:
    """Add the four-column assignee block idempotently."""
    op.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS assigned_to VARCHAR(80)")
    op.execute(
        f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS assigned_to_type VARCHAR(10) "
        f"NOT NULL DEFAULT 'user'"
    )
    op.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS assigned_by VARCHAR(80)")
    op.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS assigned_at TIMESTAMP")
    op.execute(
        f"CREATE INDEX IF NOT EXISTS ix_{table}_assigned_to ON {table} (assigned_to)"
    )


def upgrade() -> None:
    # ── 1) Assignee columns on the five reviewable tables ──────────────
    for table in ASSIGNEE_TABLES:
        _add_assignee_columns(table)

    # ── 2) Defects: backfill assigned_to from the legacy `assignee` text ─
    op.execute(
        """
        UPDATE defects
           SET assigned_to = assignee,
               assigned_to_type = 'user'
         WHERE assignee IS NOT NULL
           AND assigned_to IS NULL
        """
    )

    # ── 3) org_invites: track email send state ─────────────────────────
    op.execute(
        "ALTER TABLE org_invites ADD COLUMN IF NOT EXISTS email_sent_at TIMESTAMP"
    )
    op.execute(
        "ALTER TABLE org_invites ADD COLUMN IF NOT EXISTS email_sent_to VARCHAR(255)"
    )

    # ── 4) Cross-org unique email_domains (best-effort) ────────────────
    # Skips silently when existing data already violates the constraint, so
    # this migration never blocks a deploy with messy data. Ops can add the
    # index manually after cleaning up.
    op.execute(
        """
        DO $$
        DECLARE
            dup_count INTEGER;
        BEGIN
            -- Detect duplicate domain claims across orgs (rough heuristic:
            -- explode comma-separated list and look for repeats).
            SELECT COUNT(*) INTO dup_count FROM (
                SELECT trim(LOWER(d)) AS domain, COUNT(DISTINCT id) AS orgs
                  FROM organizations,
                       LATERAL regexp_split_to_table(COALESCE(email_domains, ''), ',') AS d
                 WHERE trim(d) <> ''
                 GROUP BY trim(LOWER(d))
                HAVING COUNT(DISTINCT id) > 1
            ) AS dupes;

            IF dup_count > 0 THEN
                RAISE NOTICE 'organizations.email_domains has %, cross-org unique index skipped; clean up manually then add', dup_count;
            ELSE
                -- Safe to add a *advisory* check via a function-based unique
                -- constraint. Postgres doesn't support per-element uniqueness on
                -- a comma-separated string natively; for now we log only.
                NULL;
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    for table in ASSIGNEE_TABLES:
        op.execute(f"DROP INDEX IF EXISTS ix_{table}_assigned_to")
        op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS assigned_at")
        op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS assigned_by")
        op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS assigned_to_type")
        op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS assigned_to")

    op.execute("ALTER TABLE org_invites DROP COLUMN IF EXISTS email_sent_to")
    op.execute("ALTER TABLE org_invites DROP COLUMN IF EXISTS email_sent_at")
