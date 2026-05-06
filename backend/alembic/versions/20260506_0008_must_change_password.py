"""must_change_password -- force first-login password rotation

Revision ID: 0008_must_change_password
Revises: 0007_rename_todo_assignee
Create Date: 2026-05-06

Adds ``users.must_change_password`` so the lifespan-seeded ``admin/admin123``
account (and any future password-reset workflow) can flag the user as needing
a forced password rotation before they can use the rest of the API.

Idempotent (``ADD COLUMN IF NOT EXISTS``) so a hand-stamped DB also
upgrades cleanly.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0008_must_change_password"
down_revision: Union[str, None] = "0007_rename_todo_assignee"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _column_exists(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    return any(
        column["name"] == column_name
        for column in sa.inspect(bind).get_columns(table_name)
    )


def upgrade() -> None:
    if not _column_exists("users", "must_change_password"):
        op.add_column(
            "users",
            sa.Column(
                "must_change_password",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            ),
        )


def downgrade() -> None:
    if _column_exists("users", "must_change_password"):
        op.drop_column("users", "must_change_password")
