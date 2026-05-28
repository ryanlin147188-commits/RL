"""llm_provider_configs.key_prefix — UI 顯示「sk-ant-***bef9」遮罩

Revision ID: 0044_llm_provider_key_prefix
Revises: 0043_llm_provider_configs
Create Date: 2026-05-28

設計:
* 加 nullable String(32) ``key_prefix`` 欄,跟 ``api_keys.key_prefix`` 概念一致
* 既有 row(0043 後但 0044 前建的)backfill 為 NULL — 前端看到 NULL
  就顯示「未設定 prefix(請重新存一次 key)」
* 不需要 ALTER 加 NOT NULL,避免歷史資料炸 migration
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0044_llm_provider_key_prefix"
down_revision: Union[str, None] = "0043_llm_provider_configs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "llm_provider_configs",
        sa.Column("key_prefix", sa.String(length=32), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("llm_provider_configs", "key_prefix")
