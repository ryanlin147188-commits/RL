"""agent_token_usage — 每次 LLM chat 的 token 用量明細

Revision ID: 0045_agent_token_usage
Revises: 0044_llm_provider_key_prefix
Create Date: 2026-05-28

風險紅線:成本可見度。每次 chat 寫一筆,前端聊天框可即時顯示「本 session 累計
$0.0421」,管理者也可查月度組織用量。

設計:
* ``cost_usd`` 用 Numeric(10,6) 避免浮點誤差(金額)
* ``organization_id`` / ``user_id`` SET NULL on delete — 不刪歷史,但 FK 不阻擋
  org / user 刪除
* ``session_id`` 暫時不加 FK,因為 agent_sessions 表 Phase 1 才會建;留索引方便撈
* 三個複合索引給最常見的查詢:org+月份、user+月份、session 累計
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0045_agent_token_usage"
down_revision: Union[str, None] = "0044_llm_provider_key_prefix"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "agent_token_usage",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("organization_id", sa.String(length=36), nullable=True),
        sa.Column("user_id", sa.String(length=36), nullable=True),
        sa.Column("session_id", sa.String(length=36), nullable=True),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("model", sa.String(length=120), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "cache_read_tokens", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "cache_write_tokens", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "cost_usd",
            sa.Numeric(precision=10, scale=6),
            nullable=False,
            server_default="0",
        ),
        sa.Column("stop_reason", sa.String(length=32), nullable=True),
        sa.Column("response_id", sa.String(length=120), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
    )

    # 單欄索引(已用 index=True 在 model 標,這裡顯式建以利精確命名)
    op.create_index(
        "ix_agent_token_usage_org", "agent_token_usage", ["organization_id"]
    )
    op.create_index("ix_agent_token_usage_user", "agent_token_usage", ["user_id"])
    op.create_index(
        "ix_agent_token_usage_session", "agent_token_usage", ["session_id"]
    )
    op.create_index(
        "ix_agent_token_usage_provider", "agent_token_usage", ["provider"]
    )
    op.create_index(
        "ix_agent_token_usage_created_at", "agent_token_usage", ["created_at"]
    )
    # 複合索引給「最近 30 天該 org 用量」這類查詢走 index-only scan
    op.create_index(
        "ix_agent_token_usage_org_created",
        "agent_token_usage",
        ["organization_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_agent_token_usage_org_created", table_name="agent_token_usage"
    )
    op.drop_index(
        "ix_agent_token_usage_created_at", table_name="agent_token_usage"
    )
    op.drop_index("ix_agent_token_usage_provider", table_name="agent_token_usage")
    op.drop_index("ix_agent_token_usage_session", table_name="agent_token_usage")
    op.drop_index("ix_agent_token_usage_user", table_name="agent_token_usage")
    op.drop_index("ix_agent_token_usage_org", table_name="agent_token_usage")
    op.drop_table("agent_token_usage")
