"""llm_provider_configs — 每 org × provider 各一筆設定(api_key Fernet 加密)

Revision ID: 0043_llm_provider_configs
Revises: 0042_todo_schedule_id
Create Date: 2026-05-28

Phase 0 後半段:把 LLM provider API key 從 env 搬到 DB,以 Fernet 加密。
原 env 變數 (ANTHROPIC_API_KEY / OPENAI_API_KEY / GOOGLE_API_KEY) 仍保留
作為 fallback,讓部署期 bootstrap 不需要先進系統設定。

設計:
* ``organization_id`` nullable — null = 全 org 共用 default
* ``UNIQUE(organization_id, provider)`` — 每 org 每家 provider 最多一筆
* ``api_key`` 用 EncryptedString TypeDecorator(寫入自動 Fernet 加密)
* 不 cascade 級聯整個刪 — org 被刪時 row 連帶刪(ON DELETE CASCADE)
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0043_llm_provider_configs"
down_revision: Union[str, None] = "0042_todo_schedule_id"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "llm_provider_configs",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("organization_id", sa.String(length=36), nullable=True),
        sa.Column("provider", sa.String(length=32), nullable=False),
        # EncryptedString 在 DB 層是 String;明文/密文兩種長度都夠
        sa.Column("api_key", sa.String(length=500), nullable=True),
        sa.Column("base_url", sa.String(length=500), nullable=True),
        sa.Column("default_model", sa.String(length=120), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
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
        sa.UniqueConstraint(
            "organization_id", "provider", name="uq_llm_provider_org_provider"
        ),
    )
    op.create_index(
        "ix_llm_provider_configs_org",
        "llm_provider_configs",
        ["organization_id"],
    )
    op.create_index(
        "ix_llm_provider_configs_provider",
        "llm_provider_configs",
        ["provider"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_llm_provider_configs_provider", table_name="llm_provider_configs"
    )
    op.drop_index("ix_llm_provider_configs_org", table_name="llm_provider_configs")
    op.drop_table("llm_provider_configs")
