"""api_keys 表 — 長壽命 API token(CI/CD friendly)

Revision ID: 0039_api_keys
Revises: 0038_ts_poly_link
Create Date: 2026-05-26

歷史:
v1.1.10 加 API gateway,gateway 端支援 ``X-API-Key: ak_xxxxxxxxxxxxxxx``。
key 明碼 SHA256 hash 後存這張表,通過 hash 比對的 request gateway 幫忙 mint
一個 5 分鐘 JWT 給 backend 用(backend 沒接 X-API-Key,只認 JWT)。

Security:
* 只存 SHA256(key),不存明碼;明碼只在 POST 建立時回一次
* ``key_prefix`` 給 UI 展示「最後一眼能看到的前綴」(類似 GitHub PAT
  顯示 ``ghp_AbCd...``)
* ``scopes`` JSON array 可選 limit;目前 backend 沒接 scope enforcement
* ``revoked`` flag + ``expires_at`` 任一達到都立刻失效
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0039_api_keys"
down_revision: Union[str, None] = "0038_ts_poly_link"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "api_keys",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column("key_prefix", sa.String(length=16), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=True),
        sa.Column("scopes", sa.JSON(), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.Column("revoked", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key_hash"),
    )
    op.create_index("ix_api_keys_key_hash", "api_keys", ["key_hash"])
    op.create_index("ix_api_keys_user_id", "api_keys", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_api_keys_user_id", table_name="api_keys")
    op.drop_index("ix_api_keys_key_hash", table_name="api_keys")
    op.drop_table("api_keys")
