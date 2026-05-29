"""drop legacy hermes tables + users.preferred_agent

Revision ID: 0055_drop_hermes_legacy
Revises: 0054_schedule_test_round_id
Create Date: 2026-05-29

Hermes runtime 整個棄置 — Phase 1-3 的 in-process agent_service 完全自足,
從未 import Hermes。三個孤兒表(hermes_session_refs / hermes_gateway_credentials
/ hermes_memory_consents)沒有任何 production code 讀寫,只佔 schema 空間;
``users.preferred_agent`` 同樣 zero readers。一次清乾淨。

Upgrade 順序:先 drop column,再依 FK 依賴方向 drop 表(這幾張表都是 FK
指向 organizations,彼此無依賴,順序其實隨便)。
Downgrade 重建 schema 讓 chain 可逆 — 但**資料無法恢復**,如果不是新部署
不會走 downgrade,所以這個逆向是 schema-only。
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0055_drop_hermes_legacy"
down_revision: Union[str, None] = "0054_schedule_test_round_id"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def _column_exists(table: str, column: str) -> bool:
    insp = sa.inspect(op.get_bind())
    if not insp.has_table(table):
        return False
    return any(c["name"] == column for c in insp.get_columns(table))


def upgrade() -> None:
    # 1) users.preferred_agent 欄位
    if _column_exists("users", "preferred_agent"):
        op.drop_column("users", "preferred_agent")

    # 2) hermes_memory_consents
    if _table_exists("hermes_memory_consents"):
        op.drop_index(
            "ix_hermes_memory_consents_org",
            table_name="hermes_memory_consents",
        )
        op.drop_table("hermes_memory_consents")

    # 3) hermes_gateway_credentials
    if _table_exists("hermes_gateway_credentials"):
        op.drop_index(
            "ix_hermes_gateway_org",
            table_name="hermes_gateway_credentials",
        )
        op.drop_index(
            "ix_hermes_gateway_owner",
            table_name="hermes_gateway_credentials",
        )
        op.drop_table("hermes_gateway_credentials")

    # 4) hermes_session_refs
    if _table_exists("hermes_session_refs"):
        op.drop_index(
            "ix_hermes_session_refs_org",
            table_name="hermes_session_refs",
        )
        op.drop_index(
            "ix_hermes_session_refs_workspace",
            table_name="hermes_session_refs",
        )
        op.drop_index(
            "ix_hermes_session_refs_owner",
            table_name="hermes_session_refs",
        )
        op.drop_table("hermes_session_refs")


def downgrade() -> None:
    # 逆向重建空 schema(資料無法復原)— 沿用 0016 / 0017 / 0018 的欄位定義
    if not _table_exists("hermes_session_refs"):
        op.create_table(
            "hermes_session_refs",
            sa.Column("id", sa.String(64), primary_key=True),
            sa.Column("workspace_id", sa.String(64), nullable=False),
            sa.Column(
                "organization_id",
                sa.String(36),
                sa.ForeignKey("organizations.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("owner", sa.String(100), nullable=False),
            sa.Column(
                "title",
                sa.String(200),
                nullable=False,
                server_default="新對話",
            ),
            sa.Column("last_message_preview", sa.String(200), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.func.now(),
            ),
        )
        op.create_index(
            "ix_hermes_session_refs_owner",
            "hermes_session_refs",
            ["owner"],
        )
        op.create_index(
            "ix_hermes_session_refs_workspace",
            "hermes_session_refs",
            ["workspace_id"],
        )
        op.create_index(
            "ix_hermes_session_refs_org",
            "hermes_session_refs",
            ["organization_id"],
        )

    if not _table_exists("hermes_gateway_credentials"):
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
            sa.Column("bot_token", sa.String(800), nullable=True),
            sa.Column("extra_config", sa.JSON(), nullable=True),
            sa.Column(
                "enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.true(),
            ),
            sa.Column(
                "created_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(),
                nullable=False,
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

    if not _table_exists("hermes_memory_consents"):
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
                "created_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.func.now(),
            ),
        )
        op.create_index(
            "ix_hermes_memory_consents_org",
            "hermes_memory_consents",
            ["organization_id"],
        )

    if not _column_exists("users", "preferred_agent"):
        op.add_column(
            "users",
            sa.Column("preferred_agent", sa.String(40), nullable=True),
        )
