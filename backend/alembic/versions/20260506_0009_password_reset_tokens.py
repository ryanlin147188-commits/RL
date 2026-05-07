"""password_reset_tokens -- new table for forgot-password flow

Revision ID: 0009_password_reset_tokens
Revises: 0008_must_change_password
Create Date: 2026-05-06

Adds the ``password_reset_tokens`` table backing
``POST /auth/forgot-password`` → email link → ``POST /auth/reset-password``.

Idempotent: skip create if the table already exists (handles the case where
0001 baseline already created it from current Base.metadata on a fresh DB).
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0009_password_reset_tokens"
down_revision: Union[str, None] = "0008_must_change_password"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(name: str) -> bool:
    bind = op.get_bind()
    return sa.inspect(bind).has_table(name)


def upgrade() -> None:
    if _table_exists("password_reset_tokens"):
        return
    op.create_table(
        "password_reset_tokens",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("token", sa.String(64), nullable=False, unique=True),
        sa.Column(
            "username",
            sa.String(80),
            sa.ForeignKey("users.username", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("email_sent_to", sa.String(255), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("used_at", sa.DateTime(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("requested_ip", sa.String(64), nullable=True),
    )
    op.create_index(
        "ix_password_reset_tokens_username",
        "password_reset_tokens", ["username"],
    )
    op.create_index(
        "ix_password_reset_tokens_token",
        "password_reset_tokens", ["token"],
    )


def downgrade() -> None:
    if not _table_exists("password_reset_tokens"):
        return
    # DROP TABLE 在 Postgres 會 cascade 把 index 一起回收。
    op.drop_table("password_reset_tokens")
