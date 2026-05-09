"""create hermes_memory_consents

Revision ID: 0018_hermes_memory_consents
Revises: 0017_hermes_gateway_credentials
Create Date: 2026-05-10

mem0 PR3:per-user fact extraction 開關 + per-session 暫停名單。

設計決策(plan §2.3):
- 主鍵用 username(對齊既有 hermes_session_refs.owner 慣例)
- extraction_enabled 預設 True(opt-out;前端 UI 明示「啟用 = 額外消耗 token quota」)
- paused_session_ids 是 JSON {session_id: paused_until_epoch_sec};send_message
  時讀,過期項忽略(過期清理交給未來 cron)
- mem0-postgres 那邊 mem0 lib own schema lifecycle,backend Alembic 不碰

idempotent:每個 op 先 inspect。
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0018_hermes_memory_consents"
down_revision: Union[str, None] = "0017_hermes_gateway_credentials"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def upgrade() -> None:
    if _table_exists("hermes_memory_consents"):
        return
    op.create_table(
        "hermes_memory_consents",
        sa.Column("username", sa.String(100), primary_key=True),
        sa.Column(
            "organization_id",
            sa.String(36),
            sa.ForeignKey("organizations.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "extraction_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column("paused_session_ids", sa.JSON(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(), nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_hermes_memory_consents_org",
        "hermes_memory_consents",
        ["organization_id"],
    )


def downgrade() -> None:
    if _table_exists("hermes_memory_consents"):
        op.drop_table("hermes_memory_consents")
