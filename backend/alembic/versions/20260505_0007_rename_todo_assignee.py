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
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op


revision: str = "0007_rename_todo_assignee"
down_revision: Union[str, None] = "0006_multi_tenant_assignment"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column("todo_items", "assignee", new_column_name="assigned_to")
    op.alter_column("todo_items", "assignee_type", new_column_name="assigned_to_type")


def downgrade() -> None:
    op.alter_column("todo_items", "assigned_to", new_column_name="assignee")
    op.alter_column("todo_items", "assigned_to_type", new_column_name="assignee_type")
