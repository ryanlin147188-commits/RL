"""Phase 3: shadow user_id UUID columns on the 6 username-FK tables

Revision ID: 0028_user_id_shadow_cols
Revises: 0027_users_add_uuid_id
Create Date: 2026-05-21

v1.1.7 Phase 3:給每個目前用 ``username`` 當 FK 的欄位旁邊加一個 shadow
``user_id`` UUID 欄位,從 ``users.id`` 拉值(JOIN backfill)。

| table              | old col       | new col              |
|--------------------|---------------|----------------------|
| project_members    | username      | user_id              |
| project_members    | invited_by    | invited_by_user_id   |
| org_memberships    | username      | user_id              |
| org_memberships    | invited_by    | invited_by_user_id   |
| group_memberships  | username      | user_id              |
| password_reset_tokens | username   | user_id              |

shadow column 全部 nullable + 沒 FK constraint:Phase 3 只是 backfill,
完整 cutover(換 PK + drop username column)留到 Phase 7。

Phase 3 後的中間態:兩個欄位都在,application code 仍讀寫 username,
新 user_id 由 DB trigger 或 application 在 Phase 4-5 cutover 時開始
同步寫入。
"""
from typing import Union

from alembic import op
import sqlalchemy as sa


revision: str = "0028_user_id_shadow_cols"
down_revision: Union[str, None] = "0027_users_add_uuid_id"
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None


# (table, old_username_col, new_user_id_col, is_nullable_in_source)
SHADOW_PAIRS = [
    ("project_members", "username", "user_id", False),
    ("project_members", "invited_by", "invited_by_user_id", True),
    ("org_memberships", "username", "user_id", False),
    ("org_memberships", "invited_by", "invited_by_user_id", True),
    ("group_memberships", "username", "user_id", False),
    ("password_reset_tokens", "username", "user_id", False),
]


def upgrade() -> None:
    # 1) 全部 nullable 加 column。
    for table, _old, new_col, _src_nullable in SHADOW_PAIRS:
        op.add_column(
            table,
            sa.Column(new_col, sa.String(36), nullable=True),
        )

    # 2) backfill:JOIN users 把 username → users.id 拷貝過來。
    #    NULL username 的 row(只有 invited_by 可能 NULL)就讓 user_id 也 NULL。
    for table, old_col, new_col, _ in SHADOW_PAIRS:
        op.execute(
            f"""
            UPDATE {table} t
               SET {new_col} = u.id
              FROM users u
             WHERE u.username = t.{old_col}
               AND t.{old_col} IS NOT NULL
               AND t.{new_col} IS NULL
            """
        )

    # 3) index:後續 join 走 user_id 會用到。Phase 7 promote 成 FK constraint
    #    時 index 已經在,migration 比較快。
    for table, _old, new_col, _ in SHADOW_PAIRS:
        op.create_index(
            f"ix_{table}_{new_col}",
            table,
            [new_col],
        )

    # 4) sanity-check:對於 source NOT NULL 的欄位(user_id / non-invited_by),
    #    backfill 後 shadow column 也應該全有值;若有 row 沒對到 users.id
    #    就是孤兒,raise 讓 deploy 失敗,operator 去處理。
    bind = op.get_bind()
    for table, old_col, new_col, src_nullable in SHADOW_PAIRS:
        if src_nullable:
            continue
        result = bind.execute(
            sa.text(
                f"SELECT COUNT(*) FROM {table} "
                f"WHERE {old_col} IS NOT NULL AND {new_col} IS NULL"
            )
        ).scalar() or 0
        if result:
            raise RuntimeError(
                f"Phase 3 backfill 後 {table}.{new_col} 還有 {result} 筆 NULL "
                f"({old_col} 不為 NULL 但找不到對應 users.id)。請先清掉孤兒 row。"
            )


def downgrade() -> None:
    for table, _old, new_col, _ in SHADOW_PAIRS:
        op.drop_index(f"ix_{table}_{new_col}", table_name=table)
        op.drop_column(table, new_col)
