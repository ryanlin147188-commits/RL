"""recreate password_reset_tokens table (v1.1.5 revert of 0023 drop)

Revision ID: 0025_recreate_password_reset_tokens
Revises: 0024_rename_oidc_columns
Create Date: 2026-05-19

v1.1.3 (Casdoor cutover, migration 0023) drop 掉 password_reset_tokens 是
因為 Casdoor 接管忘記密碼流程。v1.1.5 把 Casdoor 換掉、本地密碼登入復活,
``POST /auth/forgot-password`` 跟 ``POST /auth/reset-password`` 兩支端點
重新生效,所以表也要建回來。

舊資料在 0023 已經 drop 不會回來;這支 migration 只重建 schema。Schema 跟
0023 ``downgrade()`` 是一致的,直接 copy 過來避免依賴順序倒掛。
"""
from typing import Union

from alembic import op
import sqlalchemy as sa


revision: str = "0025_recreate_pw_reset_tokens"
down_revision: Union[str, None] = "0024_rename_oidc_columns"
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None


def _table_exists(name: str) -> bool:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    return name in insp.get_table_names()


def upgrade() -> None:
    if _table_exists("password_reset_tokens"):
        return
    op.create_table(
        "password_reset_tokens",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("token", sa.String(length=80), nullable=False, index=True, unique=True),
        sa.Column(
            "username",
            sa.String(length=80),
            sa.ForeignKey("users.username", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("email_sent_to", sa.String(length=255), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("used_at", sa.DateTime(), nullable=True),
        sa.Column("requested_ip", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(),
            nullable=False, server_default=sa.text("NOW()"),
        ),
    )


def downgrade() -> None:
    if _table_exists("password_reset_tokens"):
        op.drop_table("password_reset_tokens")
