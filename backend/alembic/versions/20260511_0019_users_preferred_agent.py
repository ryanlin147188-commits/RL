"""users.preferred_agent

Revision ID: 0019_users_preferred_agent
Revises: 0018_hermes_memory_consents
Create Date: 2026-05-11

加 `users.preferred_agent` 欄位儲存使用者偏好的 agent runtime
(`hermes` / `openclaw` / NULL=自動依 token 能力挑)。

Capability gating 在應用層:settings 頁的下拉只顯示「現有 token 能跑的 agent」,
但 DB 不約束(避免使用者刪光 token 後讀不到舊偏好)。

idempotent。
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0019_users_preferred_agent"
down_revision: Union[str, None] = "0018_hermes_memory_consents"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _column_exists(table: str, column: str) -> bool:
    insp = sa.inspect(op.get_bind())
    if not insp.has_table(table):
        return False
    return column in {c["name"] for c in insp.get_columns(table)}


def upgrade() -> None:
    if not _column_exists("users", "preferred_agent"):
        op.add_column(
            "users",
            sa.Column("preferred_agent", sa.String(40), nullable=True),
        )


def downgrade() -> None:
    if _column_exists("users", "preferred_agent"):
        op.drop_column("users", "preferred_agent")
