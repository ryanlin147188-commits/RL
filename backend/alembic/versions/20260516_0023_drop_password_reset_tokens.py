"""drop password_reset_tokens — Casdoor 接管忘記密碼流程

Revision ID: 0023_drop_password_reset_tokens
Revises: 0022_drop_oidc_providers
Create Date: 2026-05-16

Phase 5 of the Casdoor + Casbin migration plan。``/api/auth/forgot-password``
/ ``/api/auth/reset-password`` 兩支端點在 Phase 5.A 改成 410,改密 / 忘記密碼
全部走 Casdoor 內建流程(``/casdoor/forget/<app>``)。此 migration 把
``password_reset_tokens`` 整張表 drop 掉。

注意:``Role.permissions_json`` **不**在此 migration 一起 drop。它是 Casdoor
切換期間的 fallback(``require_casbin`` 在 ``CASBIN_ENABLED=False`` 時退回
list[str] 檢查仍要讀),保留欄位讓 rollback 成本可控。後續若確定 Casbin
完全 stable 再開 0024 drop。

Downgrade 還原 schema 但 **資料拿不回來**;run 前請手動 ``pg_dump --table=password_reset_tokens``。
"""
from typing import Union

from alembic import op
import sqlalchemy as sa


revision: str = "0023_drop_password_reset_tokens"
down_revision: Union[str, None] = "0022_drop_oidc_providers"
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None


def _table_exists(name: str) -> bool:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    return name in insp.get_table_names()


def upgrade() -> None:
    if _table_exists("password_reset_tokens"):
        op.drop_table("password_reset_tokens")


def downgrade() -> None:
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
