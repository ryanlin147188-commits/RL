"""pending_actions + agent_messages.pending_action_id — Phase 1c-2 二次確認

Revision ID: 0048_pending_actions
Revises: 0047_agent_message_task_id
Create Date: 2026-05-28

二次確認紅線:requires_confirmation 的 tool 派出後不直接執行,寫一筆
pending_actions row,等使用者 approve / reject。agent_messages 加一欄
FK 指回去,approve 時可以直接 UPDATE 原 tool message 的 content。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0048_pending_actions"
down_revision: Union[str, None] = "0047_agent_message_task_id"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "pending_actions",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("session_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("tool_call_id", sa.String(length=120), nullable=False),
        sa.Column("tool_name", sa.String(length=80), nullable=False),
        sa.Column("arguments", sa.JSON(), nullable=True),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["session_id"], ["agent_sessions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
    )
    op.create_index(
        "ix_pending_actions_session", "pending_actions", ["session_id"]
    )
    op.create_index("ix_pending_actions_user", "pending_actions", ["user_id"])
    op.create_index(
        "ix_pending_actions_status", "pending_actions", ["status"]
    )
    op.create_index(
        "ix_pending_actions_tool", "pending_actions", ["tool_name"]
    )

    op.add_column(
        "agent_messages",
        sa.Column("pending_action_id", sa.String(length=36), nullable=True),
    )
    op.create_index(
        "ix_agent_messages_pending_action_id",
        "agent_messages",
        ["pending_action_id"],
    )
    op.create_foreign_key(
        "fk_agent_messages_pending_action_id",
        "agent_messages",
        "pending_actions",
        ["pending_action_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_agent_messages_pending_action_id",
        "agent_messages",
        type_="foreignkey",
    )
    op.drop_index(
        "ix_agent_messages_pending_action_id", table_name="agent_messages"
    )
    op.drop_column("agent_messages", "pending_action_id")

    op.drop_index("ix_pending_actions_tool", table_name="pending_actions")
    op.drop_index("ix_pending_actions_status", table_name="pending_actions")
    op.drop_index("ix_pending_actions_user", table_name="pending_actions")
    op.drop_index("ix_pending_actions_session", table_name="pending_actions")
    op.drop_table("pending_actions")
