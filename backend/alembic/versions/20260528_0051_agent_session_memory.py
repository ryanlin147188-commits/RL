"""agent_sessions.memory_enabled — mem0 per-session opt-out

Revision ID: 0051_agent_session_memory
Revises: 0050_llm_thinking_config
Create Date: 2026-05-28

預設 True — 新 session 開啟 mem0;user 在 UI 可 toggle off。既有 row
自動套 server_default=true,不需要 backfill。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0051_agent_session_memory"
down_revision: Union[str, None] = "0050_llm_thinking_config"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "agent_sessions",
        sa.Column(
            "memory_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )


def downgrade() -> None:
    op.drop_column("agent_sessions", "memory_enabled")
