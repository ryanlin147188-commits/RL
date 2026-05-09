"""create hermes_gateway_credentials

Revision ID: 0017_hermes_gateway_credentials
Revises: 0016_hermes_session_refs
Create Date: 2026-05-09

Gateway PR:per-user / per-platform 的 messaging bot token storage(Fernet 加密)。
從 Telegram 開始,後續加 discord / slack / matrix 等都是新增 row,schema 不變。

Unique constraint:同一個使用者一個平台只能有一筆,避免「我的 telegram bot 是哪
一個 token」的混淆。

idempotent:每個 op 先 inspect。
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0017_hermes_gateway_credentials"
down_revision: Union[str, None] = "0016_hermes_session_refs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def upgrade() -> None:
    if _table_exists("hermes_gateway_credentials"):
        return
    op.create_table(
        "hermes_gateway_credentials",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "organization_id",
            sa.String(36),
            sa.ForeignKey("organizations.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("owner", sa.String(100), nullable=False),
        sa.Column("platform", sa.String(40), nullable=False),
        # Fernet 加密 token;EncryptedString descriptor 攤平成 String column
        sa.Column("bot_token", sa.String(800), nullable=True),
        sa.Column("extra_config", sa.JSON(), nullable=True),
        sa.Column(
            "enabled", sa.Boolean(), nullable=False, server_default=sa.true(),
        ),
        sa.Column(
            "created_at", sa.DateTime(), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "owner", "platform", name="uq_hermes_gateway_owner_platform",
        ),
    )
    op.create_index(
        "ix_hermes_gateway_owner",
        "hermes_gateway_credentials",
        ["owner"],
    )
    op.create_index(
        "ix_hermes_gateway_org",
        "hermes_gateway_credentials",
        ["organization_id"],
    )


def downgrade() -> None:
    if _table_exists("hermes_gateway_credentials"):
        op.drop_table("hermes_gateway_credentials")
