"""agent_sessions + agent_messages — 對話與訊息歷史

Revision ID: 0046_agent_sessions_messages
Revises: 0045_agent_token_usage
Create Date: 2026-05-28

Phase 1a:讓使用者能在後端建一個對話,送一條訊息,LLM 回應並寫回 DB。
``tool_calls`` / ``tool_call_id`` 欄位 Phase 1a 不會用到但先建,避免 Phase 1b
接 tool 時又一次 alter。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0046_agent_sessions_messages"
down_revision: Union[str, None] = "0045_agent_token_usage"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "agent_sessions",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=True),
        sa.Column("title", sa.String(length=120), nullable=True),
        sa.Column("model", sa.String(length=120), nullable=True),
        sa.Column("system_prompt", sa.Text(), nullable=True),
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
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
    )
    op.create_index("ix_agent_sessions_user", "agent_sessions", ["user_id"])
    op.create_index("ix_agent_sessions_org", "agent_sessions", ["organization_id"])
    op.create_index(
        "ix_agent_sessions_updated_at", "agent_sessions", ["updated_at"]
    )

    op.create_table(
        "agent_messages",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("session_id", sa.String(length=36), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False, server_default=""),
        sa.Column("tool_calls", sa.JSON(), nullable=True),
        sa.Column("tool_call_id", sa.String(length=120), nullable=True),
        sa.Column("token_usage_id", sa.String(length=36), nullable=True),
        sa.Column("seq", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(
            ["session_id"], ["agent_sessions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["token_usage_id"], ["agent_token_usage.id"], ondelete="SET NULL"
        ),
    )
    op.create_index("ix_agent_messages_session", "agent_messages", ["session_id"])
    op.create_index(
        "ix_agent_messages_created_at", "agent_messages", ["created_at"]
    )
    # 複合索引給「列某 session 的訊息 by seq」最常見查詢
    op.create_index(
        "ix_agent_messages_session_seq",
        "agent_messages",
        ["session_id", "seq"],
    )


def downgrade() -> None:
    op.drop_index("ix_agent_messages_session_seq", table_name="agent_messages")
    op.drop_index("ix_agent_messages_created_at", table_name="agent_messages")
    op.drop_index("ix_agent_messages_session", table_name="agent_messages")
    op.drop_table("agent_messages")

    op.drop_index("ix_agent_sessions_updated_at", table_name="agent_sessions")
    op.drop_index("ix_agent_sessions_org", table_name="agent_sessions")
    op.drop_index("ix_agent_sessions_user", table_name="agent_sessions")
    op.drop_table("agent_sessions")
