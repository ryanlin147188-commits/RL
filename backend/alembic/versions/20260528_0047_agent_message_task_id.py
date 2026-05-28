"""agent_messages.task_id — 非同步 tool 派 Celery 後存 task_id

Revision ID: 0047_agent_message_task_id
Revises: 0046_agent_sessions_messages
Create Date: 2026-05-28

Phase 1c-1:非同步 tool(``run_test_case`` 等)派 Celery 之後,tool 訊息存的
是「已排程, task_id=X」這類占位 content;真正結果由 Celery worker 在另一個
process 跑完。前端拿 task_id 去 GET /api/executions/{task_id}/status 輪詢,
或 Phase 1c-2 起接 WS。

加索引方便 Celery 完成事件回流時用 task_id 反查對應 message。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0047_agent_message_task_id"
down_revision: Union[str, None] = "0046_agent_sessions_messages"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "agent_messages",
        sa.Column("task_id", sa.String(length=36), nullable=True),
    )
    op.create_index(
        "ix_agent_messages_task_id", "agent_messages", ["task_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_agent_messages_task_id", table_name="agent_messages")
    op.drop_column("agent_messages", "task_id")
