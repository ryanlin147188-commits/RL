"""mcp_servers + mcp_server_secrets + mcp_tools_cache — Phase 2b MCP client

Revision ID: 0053_mcp_servers
Revises: 0052_skills
Create Date: 2026-05-29

per-org MCP server 設定 + 加密 secret + tools 快取。Phase 2 只跑 streamable
HTTP transport;stdio 欄位先預留(Phase 3 才實作)。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0053_mcp_servers"
down_revision: Union[str, None] = "0052_skills"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "mcp_servers",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("organization_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("transport", sa.String(length=16), nullable=False),
        sa.Column("command", sa.String(length=512), nullable=True),
        sa.Column("args_json", sa.JSON(), nullable=True),
        sa.Column("url", sa.String(length=512), nullable=True),
        sa.Column("env_json", sa.JSON(), nullable=True),
        sa.Column("headers_json", sa.JSON(), nullable=True),
        sa.Column(
            "enabled", sa.Boolean(), nullable=False, server_default="true"
        ),
        sa.Column(
            "requires_confirmation",
            sa.Boolean(),
            nullable=False,
            server_default="true",
        ),
        sa.Column("casbin_permission", sa.String(length=64), nullable=True),
        sa.Column(
            "last_health",
            sa.String(length=16),
            nullable=False,
            server_default="unknown",
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(), nullable=True),
        sa.Column("created_by", sa.String(length=36), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], ondelete="SET NULL"
        ),
        sa.UniqueConstraint(
            "organization_id", "name", name="uq_mcp_servers_org_name"
        ),
        sa.CheckConstraint(
            "transport IN ('stdio','http')",
            name="ck_mcp_servers_transport",
        ),
    )
    op.create_index("ix_mcp_servers_org", "mcp_servers", ["organization_id"])
    op.create_index("ix_mcp_servers_name", "mcp_servers", ["name"])

    op.create_table(
        "mcp_server_secrets",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("server_id", sa.String(length=36), nullable=False),
        sa.Column("ref_name", sa.String(length=64), nullable=False),
        # EncryptedString 底層是 String(2048);DB schema 用 sa.String 即可
        sa.Column("value", sa.String(length=2048), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(
            ["server_id"], ["mcp_servers.id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint(
            "server_id", "ref_name", name="uq_mcp_secret_server_ref"
        ),
    )
    op.create_index(
        "ix_mcp_server_secrets_server", "mcp_server_secrets", ["server_id"]
    )

    op.create_table(
        "mcp_tools_cache",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("server_id", sa.String(length=36), nullable=False),
        sa.Column("tool_name", sa.String(length=120), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("input_schema", sa.JSON(), nullable=False),
        sa.Column(
            "fetched_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(
            ["server_id"], ["mcp_servers.id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint(
            "server_id", "tool_name", name="uq_mcp_tool_server_name"
        ),
    )
    op.create_index(
        "ix_mcp_tools_cache_server", "mcp_tools_cache", ["server_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_mcp_tools_cache_server", table_name="mcp_tools_cache")
    op.drop_table("mcp_tools_cache")
    op.drop_index(
        "ix_mcp_server_secrets_server", table_name="mcp_server_secrets"
    )
    op.drop_table("mcp_server_secrets")
    op.drop_index("ix_mcp_servers_name", table_name="mcp_servers")
    op.drop_index("ix_mcp_servers_org", table_name="mcp_servers")
    op.drop_table("mcp_servers")
