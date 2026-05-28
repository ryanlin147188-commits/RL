"""agent_sessions.mode — Phase 2 路線 B 自主 Agent 模式切換

Revision ID: 0049_agent_session_mode
Revises: 0048_pending_actions
Create Date: 2026-05-28

* "chat"   — Phase 1 預設,使用者自由對話
* "planner" — Phase 2 路線 B:吃需求文字 → LLM 設計測試案例
* "analyzer" — Phase 2 路線 B:吃 failed execution_report → LLM 分析 root cause

server_default="chat" 讓既有 row 升級時自動填,不需要 backfill。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0049_agent_session_mode"
down_revision: Union[str, None] = "0048_pending_actions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "agent_sessions",
        sa.Column(
            "mode",
            sa.String(length=16),
            nullable=False,
            server_default="chat",
        ),
    )
    op.create_index("ix_agent_sessions_mode", "agent_sessions", ["mode"])


def downgrade() -> None:
    op.drop_index("ix_agent_sessions_mode", table_name="agent_sessions")
    op.drop_column("agent_sessions", "mode")
