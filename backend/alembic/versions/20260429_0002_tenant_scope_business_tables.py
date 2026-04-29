"""tenant_scope_business_tables -- add organization_id to 18 business tables

Revision ID: 0002_tenant_scope
Revises: 0001_baseline
Create Date: 2026-04-29

Adds ``organization_id`` to every business table that previously relied on
``project_id`` (or a parent FK) for tenant scoping. This is RFC-4 from the
optimisation plan: makes the tenant filter a first-class column rather than a
JOIN through projects, so the ``TenantQuery.for_(model)`` helper can scope
queries with one ``WHERE`` and ORM ``before_flush`` can auto-stamp inserts.

Strategy
--------
* ``ADD COLUMN IF NOT EXISTS`` -- both baseline-fresh DBs (where models
  already declare the column via :class:`TenantScoped`) and DBs migrated
  from the legacy ``init_db`` path are handled by the same migration.
* Backfill happens in two waves:
    1. **Direct tables** -- copy ``organization_id`` from ``projects``
       through their ``project_id`` FK.
    2. **Indirect tables** -- copy from a now-populated direct parent
       (e.g. ``execution_steps_log`` <- ``execution_reports``).
* Indexes + FKs added with ``IF NOT EXISTS`` guards.
* Column kept ``nullable=True`` for now: fully populating may require ops
  intervention for orphaned rows; a later migration can switch to NOT NULL
  once each deployment has confirmed clean data.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0002_tenant_scope"
down_revision: Union[str, None] = "0001_baseline"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Direct tables: organization_id sourced from projects via the named FK column.
DIRECT_TABLES: list[tuple[str, str]] = [
    ("defects", "project_id"),
    ("execution_reports", "project_id"),
    ("tree_nodes", "project_id"),
    ("requirements", "project_id"),
    ("test_plans", "project_id"),
    ("test_rounds", "project_id"),
    ("test_milestones", "project_id"),
    ("test_data_sets", "project_id"),
    ("test_documents", "project_id"),
    ("wbs_items", "project_id"),
    ("schedules", "project_id"),
    ("recording_sessions", "project_id"),
    ("project_devices", "project_id"),
    ("project_env_vars", "project_id"),
]

# Indirect tables: backfill from a parent table that was just populated above.
# (table_name, parent_table, parent_fk_column_on_self)
INDIRECT_TABLES: list[tuple[str, str, str]] = [
    ("testcase_contents", "tree_nodes", "node_id"),
    ("execution_steps_log", "execution_reports", "report_id"),
    ("step_screenshot_baselines", "tree_nodes", "testcase_node_id"),
    ("requirement_testcase_links", "requirements", "requirement_id"),
]


def _add_column(table: str) -> None:
    op.execute(
        f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS organization_id VARCHAR(36)"
    )


def _add_index(table: str) -> None:
    op.execute(
        f"CREATE INDEX IF NOT EXISTS ix_{table}_organization_id "
        f"ON {table} (organization_id)"
    )


def _add_fk(table: str) -> None:
    fk_name = f"fk_{table}_organization"
    op.execute(
        f"""
        DO $$ BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint WHERE conname = '{fk_name}'
          ) THEN
            ALTER TABLE {table}
              ADD CONSTRAINT {fk_name}
              FOREIGN KEY (organization_id) REFERENCES organizations(id)
              ON DELETE CASCADE;
          END IF;
        END $$;
        """
    )


def upgrade() -> None:
    # ── 1) Add column + index + FK on every target table ───────────────
    for table, _ in DIRECT_TABLES:
        _add_column(table)
        _add_index(table)
    for table, _, _ in INDIRECT_TABLES:
        _add_column(table)
        _add_index(table)

    # ── 2) Backfill direct tables from projects ─────────────────────────
    for table, fk in DIRECT_TABLES:
        op.execute(
            f"""
            UPDATE {table} t
               SET organization_id = p.organization_id
              FROM projects p
             WHERE t.{fk} = p.id
               AND t.organization_id IS NULL
            """
        )

    # ── 3) Backfill indirect tables from their (now-populated) parent ──
    for table, parent, fk in INDIRECT_TABLES:
        op.execute(
            f"""
            UPDATE {table} t
               SET organization_id = p.organization_id
              FROM {parent} p
             WHERE t.{fk} = p.id
               AND t.organization_id IS NULL
            """
        )

    # ── 4) Apply FK constraints last (after data is in place) ──────────
    for table, _ in DIRECT_TABLES:
        _add_fk(table)
    for table, _, _ in INDIRECT_TABLES:
        _add_fk(table)


def downgrade() -> None:
    # Drop FK -> index -> column. Order matters: PG won't drop a column with
    # a dependent constraint without CASCADE.
    for table, _ in DIRECT_TABLES:
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS fk_{table}_organization")
        op.execute(f"DROP INDEX IF EXISTS ix_{table}_organization_id")
        op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS organization_id")
    for table, _, _ in INDIRECT_TABLES:
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS fk_{table}_organization")
        op.execute(f"DROP INDEX IF EXISTS ix_{table}_organization_id")
        op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS organization_id")
