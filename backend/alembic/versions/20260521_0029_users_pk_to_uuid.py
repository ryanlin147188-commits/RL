"""users: promote id (UUID) to PK; username stays UNIQUE NOT NULL

Revision ID: 0029_users_pk_to_uuid
Revises: 0028_user_id_shadow_cols
Create Date: 2026-05-21

v1.1.7 Phase 7: PK 從 ``username`` 換成 ``id``(UUID)。為了不必同時改 6
個 FK 表的 constraint(會影響 30+ 個 application file 跟 SPA 100+ URL),
採取「最小侵入」方案:

* ``users.username`` 保留為 ``NOT NULL UNIQUE``(原本 PK 也是 unique,沒
  變),既有 6 個 ``ForeignKey("users.username")`` 全部繼續有效。
* ``users.id`` 升格成 PK。Phase 5 dual-write listener 已經確保新 row 也有
  ``user_id`` UUID,fastapi-users 的 ``SQLAlchemyUserDatabase.get(id)`` 走
  PK lookup 走得通。
* ``user_id`` shadow columns 暫時保持 nullable + no FK;Phase 8 才會加
  ``FOREIGN KEY ... REFERENCES users(id)``。

application code(JWT sub / Casbin subject / SPA URL /api/auth/users/
{username})繼續用 username 識別,**不變**。
"""
from typing import Union

from alembic import op
import sqlalchemy as sa


revision: str = "0029_users_pk_to_uuid"
down_revision: Union[str, None] = "0028_user_id_shadow_cols"
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None


# 6 個 FK constraints 直接綁在 users_pkey 上(Postgres FK 預設指向 PK,即使
# REFERENCES users(username) — pg 內部仍 link 到 PK constraint OID)。要換 PK
# 必須先 drop 它們、換完再重建,**重建時 REFERENCES 顯式寫 username** 就會
# 指向新的 unique constraint。
FK_CONSTRAINTS = [
    ("group_memberships", "group_memberships_username_fkey", "username", "CASCADE"),
    ("org_memberships", "org_memberships_username_fkey", "username", "CASCADE"),
    ("org_memberships", "org_memberships_invited_by_fkey", "invited_by", "SET NULL"),
    ("project_members", "project_members_username_fkey", "username", "CASCADE"),
    ("project_members", "project_members_invited_by_fkey", "invited_by", "SET NULL"),
    ("password_reset_tokens", "password_reset_tokens_username_fkey", "username", "CASCADE"),
]


def upgrade() -> None:
    # 1) 釘 username UNIQUE — 後面 FK 重建會 REFERENCES users(username),
    #    需要一個 unique constraint 可指。
    op.create_unique_constraint("uq_users_username", "users", ["username"])

    # 2) 拔 6 個依賴 users_pkey 的 FK constraint。
    for table, name, _col, _on_delete in FK_CONSTRAINTS:
        op.drop_constraint(name, table, type_="foreignkey")

    # 3) ``id`` 之前是 unique index(0027 加的);drop 它,讓 PK 自己建 unique。
    op.drop_index("ix_users_id_unique", table_name="users")

    # 4) 拔 username PK,加 id PK。
    op.drop_constraint("users_pkey", "users", type_="primary")
    op.create_primary_key("users_pkey", "users", ["id"])

    # 5) 重建 6 個 FK constraint,顯式指 users(username);Postgres 現在會把
    #    它 link 到 uq_users_username 而不是 users_pkey。
    for table, name, col, on_delete in FK_CONSTRAINTS:
        op.create_foreign_key(
            name,
            table,
            "users",
            [col],
            ["username"],
            ondelete=on_delete,
        )


def downgrade() -> None:
    # 反向順序:drop 6 FK → swap PK 回 username → 重建 6 FK → 拔 uq
    for table, name, _col, _on_delete in FK_CONSTRAINTS:
        op.drop_constraint(name, table, type_="foreignkey")
    op.drop_constraint("users_pkey", "users", type_="primary")
    op.create_primary_key("users_pkey", "users", ["username"])
    op.create_index("ix_users_id_unique", "users", ["id"], unique=True)
    for table, name, col, on_delete in FK_CONSTRAINTS:
        op.create_foreign_key(
            name, table, "users", [col], ["username"], ondelete=on_delete,
        )
    op.drop_constraint("uq_users_username", "users", type_="unique")
