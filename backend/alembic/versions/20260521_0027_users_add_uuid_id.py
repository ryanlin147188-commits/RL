"""users: add UUID id column (Phase 2 of fastapi-users migration)

Revision ID: 0027_users_add_uuid_id
Revises: 0026_project_role_permissions
Create Date: 2026-05-21

v1.1.7 Phase 2: 加 ``users.id`` UUID 欄位,但**不**動 PK。

* username 仍是 PK,給既有 6 個 FK table(project_members /
  org_memberships / groups / password_reset_tokens 等)沿用。
* 新 ``id`` UUID 欄位:fastapi-users 需要;Phase 3 開始給其他 table
  加 ``user_id UUID`` 欄位 + backfill,Phase 7 才會把 PK 換成 id。

backfill 邏輯:既有 row 在 column add 後立刻塞一個新 UUID;之後新建
user 由 application 層(server_default=gen_random_uuid())或 ORM
default 帶入。

Postgres ``gen_random_uuid()`` 是 pgcrypto extension 提供;v1.1.5
之前的 schema 已經 enable 過(audit_logs 等表用過)。如果還沒,先
``CREATE EXTENSION IF NOT EXISTS pgcrypto``。
"""
from typing import Union

from alembic import op
import sqlalchemy as sa


revision: str = "0027_users_add_uuid_id"
down_revision: Union[str, None] = "0026_project_role_permissions"
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None


def upgrade() -> None:
    # pgcrypto:gen_random_uuid 用;idempotent。
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    # 1) 加 column,先 nullable 再 backfill。
    op.add_column(
        "users",
        sa.Column("id", sa.String(36), nullable=True),
    )

    # 2) 既有 row 一律塞新 UUID。gen_random_uuid() 已對齊 36 字元 string 表示。
    op.execute("UPDATE users SET id = gen_random_uuid()::text WHERE id IS NULL")

    # 3) 設 NOT NULL + server_default,新建 row 自動補。
    op.alter_column(
        "users",
        "id",
        nullable=False,
        server_default=sa.text("gen_random_uuid()::text"),
    )

    # 4) UNIQUE index,Phase 7 直接 promote 成 PK。
    op.create_index(
        "ix_users_id_unique",
        "users",
        ["id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_users_id_unique", table_name="users")
    op.drop_column("users", "id")
