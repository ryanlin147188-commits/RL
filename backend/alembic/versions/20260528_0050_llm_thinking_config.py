"""llm_provider_configs.thinking_config — 統一 thinking level 設定

Revision ID: 0050_llm_thinking_config
Revises: 0049_agent_session_mode
Create Date: 2026-05-28

加 JSON 欄存統一思考度設定。格式約定:
    {"level": "off" | "low" | "medium" | "high"}

backend chat 時把 level 翻成各家對應參數(Anthropic budget_tokens /
OpenAI reasoning_effort / Google thinkingBudget)。nullable,既有 row 升級
時自動為 NULL = 不啟用 thinking。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0050_llm_thinking_config"
down_revision: Union[str, None] = "0049_agent_session_mode"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "llm_provider_configs",
        sa.Column("thinking_config", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("llm_provider_configs", "thinking_config")
