"""unify_status -- 4 entity status enum 全部統一成 7 值

Revision ID: 0011_unify_status
Revises: 0010_entity_versions
Create Date: 2026-05-06

把 defect / todo / requirement / review 的 status 欄位轉成統一的 7 個值:
``New / Assigned / InProgress / InReview / ReworkRequired / Verified / Closed``

設計:
  * 把每個 status 欄位從 PG native enum 轉成 ``VARCHAR(20)``,讓 Python 端
    enum class 統一管枚舉值,DB 不再寫死(避免之後改 enum 又要 ALTER TYPE)。
  * UPDATE 既有資料:把舊值對應到新值(see _STATUS_REMAP)。
  * DROP 舊的 PG enum type(如果存在)。

idempotent:
  * 若 column type 已經是 VARCHAR(fresh DB 0001 baseline 用 new model 建表)
    則 ALTER 是 no-op cast,UPDATE 也碰不到任何 row,DROP TYPE IF EXISTS 不爆。
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0011_unify_status"
down_revision: Union[str, None] = "0010_entity_versions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# (table, column, old_enum_type_name, {old_value: new_value} 映射)
_TABLES = [
    (
        "defects",
        "status",
        "defectstatus",
        {"Fixed": "InReview", "Reopened": "ReworkRequired", "WontFix": "Closed"},
    ),
    (
        "todo_items",
        "status",
        "todostatus",
        {"Todo": "Assigned", "Done": "Verified", "Cancelled": "Closed"},
    ),
    (
        "requirements",
        "status",
        "requirementstatus",
        {"Draft": "New", "Approved": "Assigned", "Implemented": "InReview", "Deprecated": "Closed"},
    ),
    (
        # ReviewStatus 是顯式命名 review_status,且 review_records + review_history 兩張表都用到
        "review_records",
        "status",
        "review_status",
        {"pending": "InReview", "approved": "Verified", "rejected": "Closed"},
    ),
]


def _column_type_is_varchar(table: str, column: str) -> bool:
    """直接查 information_schema 判斷實際型別,避免 SQLAlchemy 反射對 ENUM 回報失準。"""
    bind = op.get_bind()
    row = bind.execute(
        sa.text(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name=:t AND column_name=:c"
        ).bindparams(t=table, c=column)
    ).scalar()
    if row is None:
        return False
    dt = str(row).lower()
    return "character varying" in dt or "varchar" in dt or dt == "text"


def _alter_to_varchar(table: str, column: str) -> None:
    """把 column 從 PG enum 轉 VARCHAR(20)。已經是 VARCHAR 就 no-op。"""
    if _column_type_is_varchar(table, column):
        return
    op.execute(
        f'ALTER TABLE {table} ALTER COLUMN {column} TYPE VARCHAR(20) USING {column}::text'
    )


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # Phase 1: 全部 ALTER + UPDATE,把每個欄位從 enum 轉成 VARCHAR 並更新值
    # (DROP TYPE 必須等所有依賴此 enum 的欄位都轉走後才能執行,所以延後到 phase 2)
    for table, column, enum_name, remap in _TABLES:
        if not inspector.has_table(table):
            continue
        _alter_to_varchar(table, column)
        for old, new in remap.items():
            op.execute(
                sa.text(
                    f'UPDATE {table} SET {column} = :new WHERE {column} = :old'
                ).bindparams(old=old, new=new)
            )

    # review_history 也有 previous_status / new_status 兩個 review_status 欄;
    # 必須在 DROP TYPE review_status 之前先轉走。
    if inspector.has_table("review_history"):
        for col in ("previous_status", "new_status"):
            _alter_to_varchar("review_history", col)
            for old, new in {"pending": "InReview", "approved": "Verified", "rejected": "Closed"}.items():
                op.execute(
                    sa.text(
                        f'UPDATE review_history SET {col} = :new WHERE {col} = :old'
                    ).bindparams(old=old, new=new)
                )

    # Phase 2: Drop 舊的 PG enum type(若還存在);此時已無欄位依賴。
    for _, _, enum_name, _ in _TABLES:
        op.execute(f'DROP TYPE IF EXISTS {enum_name}')


def downgrade() -> None:
    # 一律 no-op:downgrade 把舊狀態值反向 map 回去意義不大
    # (Fixed / Reopened / Done / Draft / pending 等舊值已被 UPDATE 蓋掉,
    # 沒有「明確 reversible」的回滾路徑)。
    pass
