"""unify_status_part2 -- 把 test_plan / test_milestone / wbs / project 也統一成 7 值

Revision ID: 0012_unify_status_part2
Revises: 0011_unify_status
Create Date: 2026-05-06

繼 0011 (defect / todo / requirement / review) 之後,把剩下 4 個 entity 的
status 也統一成:``New / Assigned / InProgress / InReview / ReworkRequired / Verified / Closed``

設計與 0011 相同:
  * column 改 ``VARCHAR(20)`` 由 Python enum 管枚舉值。
  * UPDATE 既有資料(_STATUS_REMAP)。
  * DROP 舊的 PG enum type。

idempotent:fresh DB 從 baseline 走最新 model 建表,ALTER 是 no-op,UPDATE 不碰任何 row。
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0012_unify_status_part2"
down_revision: Union[str, None] = "0011_unify_status"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# (table, column, old_enum_type_name, {old_value: new_value})
_TABLES = [
    (
        "test_plans",
        "status",
        "testplanstatus",
        {"Draft": "New", "Approved": "Verified", "Active": "InProgress"},
    ),
    (
        "test_milestones",
        "status",
        "milestonestatus",
        {"Planned": "New", "Completed": "Verified", "Cancelled": "Closed"},
    ),
    (
        "wbs_items",
        "status",
        "wbsstatus",
        {"NotStarted": "New", "Completed": "Verified", "Blocked": "ReworkRequired", "Cancelled": "Closed"},
    ),
    # projects.status 本來就是 VARCHAR(40)(自由字串),不需要 ALTER TYPE,只 UPDATE 值
    (
        "projects",
        "status",
        None,
        {"Planning": "New", "Active": "InProgress", "OnHold": "Assigned", "Archived": "Closed"},
    ),
]


def _column_type_is_varchar(table: str, column: str) -> bool:
    bind = op.get_bind()
    cols = sa.inspect(bind).get_columns(table)
    for c in cols:
        if c["name"] == column:
            type_str = str(c["type"]).upper()
            return "VARCHAR" in type_str or "CHARACTER VARYING" in type_str
    return False


def _alter_to_varchar(table: str, column: str) -> None:
    if _column_type_is_varchar(table, column):
        return
    op.execute(
        f'ALTER TABLE {table} ALTER COLUMN {column} TYPE VARCHAR(20) USING {column}::text'
    )


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    for table, column, enum_name, remap in _TABLES:
        if not inspector.has_table(table):
            continue
        # projects.status 已是 VARCHAR(40),skip ALTER;其餘走 enum→VARCHAR
        if enum_name is not None:
            _alter_to_varchar(table, column)
        for old, new in remap.items():
            op.execute(
                sa.text(
                    f'UPDATE {table} SET {column} = :new WHERE {column} = :old'
                ).bindparams(old=old, new=new)
            )
        if enum_name is not None:
            op.execute(f'DROP TYPE IF EXISTS {enum_name}')


def downgrade() -> None:
    # 同 0011:no-op,不做反向 mapping
    pass
