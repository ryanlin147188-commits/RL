"""rename_todo_assignee -- align TodoItem with the rest of Assignable mixin

Revision ID: 0007_rename_todo_assignee
Revises: 0006_multi_tenant_assignment
Create Date: 2026-05-05

TodoItem was the original assignment target (Phase 1) and used `assignee` /
`assignee_type` columns. When the generic Assignable mixin shipped (Phase 2,
migration 0004), it picked the more specific names `assigned_to` /
`assigned_to_type` and applied them to Defect / Review / TestCase / Requirement /
TestDocument.

That schism cost us:
  * Two parallel code paths in the routers (todos.py vs assignments.py)
  * Group fan-out only working for TodoItem (assignments.py:128 deferred to
    "v1.2"), even though the BFS helper exists
  * Awkward dict-mapping every time we serialise either kind

This rename brings TodoItem in line. Pure column rename — no data loss, no
constraint changes, and reversible.

Idempotency notes (fresh-DB bootstrap):
  * 0001 baseline runs ``Base.metadata.create_all`` against the *current*
    model, so todo_items is born with ``assigned_to`` / ``assigned_to_type``.
  * 0005 still issues ``ADD COLUMN IF NOT EXISTS assignee_type`` for the
    legacy lightweight-SQL path, so on fresh DBs we end up with an orphan
    ``assignee_type`` next to the canonical ``assigned_to_type``.
  * This revision must therefore handle: (a) real upgrade — rename
    legacy → canonical; (b) fresh DB — drop the orphan; (c) any half-state.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0007_rename_todo_assignee"
down_revision: Union[str, None] = "0006_multi_tenant_assignment"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _column_exists(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    return any(
        column["name"] == column_name
        for column in sa.inspect(bind).get_columns(table_name)
    )


def _normalize(legacy: str, canonical: str) -> None:
    """Bring ``todo_items`` into the canonical-only state for one column pair."""
    legacy_exists = _column_exists("todo_items", legacy)
    canonical_exists = _column_exists("todo_items", canonical)

    if legacy_exists and not canonical_exists:
        op.alter_column("todo_items", legacy, new_column_name=canonical)
    elif legacy_exists and canonical_exists:
        # Fresh-DB path: 0005 left an orphan legacy column next to the
        # canonical one created by 0001. Drop the orphan.
        op.drop_column("todo_items", legacy)
    # else: canonical-only (already migrated) or neither (shouldn't happen) — no-op


def upgrade() -> None:
    _normalize("assignee", "assigned_to")
    _normalize("assignee_type", "assigned_to_type")


def downgrade() -> None:
    if _column_exists("todo_items", "assigned_to") and not _column_exists(
        "todo_items", "assignee"
    ):
        op.alter_column("todo_items", "assigned_to", new_column_name="assignee")
    if _column_exists("todo_items", "assigned_to_type") and not _column_exists(
        "todo_items", "assignee_type"
    ):
        op.alter_column(
            "todo_items", "assigned_to_type", new_column_name="assignee_type"
        )
