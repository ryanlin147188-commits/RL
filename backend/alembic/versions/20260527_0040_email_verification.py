"""email_verification_tokens 表 — 自助註冊 email 驗證

Revision ID: 0040_email_verification
Revises: 0039_api_keys
Create Date: 2026-05-27

歷史:
v1.1.10 重新啟用自助註冊(``POST /api/auth/register``),但加上 email 驗證
這道防呢一直擋假信箱。User register 時建 row + ``is_active=False``,寄信給
他點連結;點完之後 ``is_active=True`` 才能登入。

設計:
- ``user_id`` FK ON DELETE CASCADE — 刪 user 拉著清掉 token
- ``token`` UNIQUE + 索引 — 驗證時直接 hit
- 24h TTL(``expires_at``)— 比 password_reset(1h)長,因為 email 偶爾會延遲
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0040_email_verification"
down_revision: Union[str, None] = "0039_api_keys"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "email_verification_tokens",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("token", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("email_sent_to", sa.String(length=255), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("used_at", sa.DateTime(), nullable=True),
        sa.Column("requested_ip", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token"),
    )
    op.create_index(
        "ix_email_verification_tokens_token",
        "email_verification_tokens",
        ["token"],
    )
    op.create_index(
        "ix_email_verification_tokens_user_id",
        "email_verification_tokens",
        ["user_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_email_verification_tokens_user_id", table_name="email_verification_tokens")
    op.drop_index("ix_email_verification_tokens_token", table_name="email_verification_tokens")
    op.drop_table("email_verification_tokens")
