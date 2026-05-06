"""fix_legacy_status_names -- 修補 0011/0012 遺漏的「SQLAlchemy enum NAME」資料

Revision ID: 0013_fix_legacy_status_names
Revises: 0012_unify_status_part2
Create Date: 2026-05-06

問題:
  0011/0012 把 status 欄位從 PG enum 轉 VARCHAR 並 UPDATE 舊值 → 新值,
  但 source 用的是 enum.value 字串(``"NotStarted"`` / ``"pending"`` 等),
  而 SQLAlchemy 預設把 enum 寫進 PG 時用的是 ``Enum.name``(``NOT_STARTED`` /
  ``PENDING`` 等大寫)。`status::text` cast 後仍是 NAME,UPDATE 全 miss,
  造成 ORM read 時 LookupError(API 回 500)。

本 migration 直接 UPDATE 舊 NAME → 新 value:
  defects:        NEW→New / ASSIGNED→Assigned / IN_PROGRESS→InProgress /
                  FIXED→InReview / REOPENED→ReworkRequired / VERIFIED→Verified /
                  CLOSED→Closed / WONT_FIX→Closed
  todo_items:     TODO→Assigned / IN_PROGRESS→InProgress / DONE→Verified /
                  CANCELLED→Closed (NEW 不存在)
  requirements:   DRAFT→New / APPROVED→Assigned / IMPLEMENTED→InReview /
                  VERIFIED→Verified / DEPRECATED→Closed
  review_records: PENDING→InReview / APPROVED→Verified / REJECTED→Closed
  review_history.previous_status / new_status: 同上
  wbs_items:      NOT_STARTED→New / IN_PROGRESS→InProgress / COMPLETED→Verified /
                  BLOCKED→ReworkRequired / CANCELLED→Closed
  test_plans:     DRAFT→New / APPROVED→Verified / ACTIVE→InProgress
  test_milestones: PLANNED→New / IN_PROGRESS→InProgress / COMPLETED→Verified /
                   CANCELLED→Closed
  projects:       Planning→New / Active→InProgress / OnHold→Assigned /
                  Archived→Closed (projects 一直就是 VARCHAR 自由字串,但仍
                  做相同 remap 以保險)

idempotent:每筆 UPDATE 條件 `WHERE status = :old`;若資料已被改成新值就 no-op。
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0013_fix_legacy_status_names"
down_revision: Union[str, None] = "0012_unify_status_part2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_REMAPS = [
    ("defects", "status", {
        "NEW": "New", "ASSIGNED": "Assigned", "IN_PROGRESS": "InProgress",
        "FIXED": "InReview", "REOPENED": "ReworkRequired", "VERIFIED": "Verified",
        "CLOSED": "Closed", "WONT_FIX": "Closed",
    }),
    ("todo_items", "status", {
        "TODO": "Assigned", "IN_PROGRESS": "InProgress",
        "DONE": "Verified", "CANCELLED": "Closed",
    }),
    ("requirements", "status", {
        "DRAFT": "New", "APPROVED": "Assigned", "IMPLEMENTED": "InReview",
        "VERIFIED": "Verified", "DEPRECATED": "Closed",
    }),
    ("review_records", "status", {
        "PENDING": "InReview", "APPROVED": "Verified", "REJECTED": "Closed",
    }),
    ("review_history", "previous_status", {
        "PENDING": "InReview", "APPROVED": "Verified", "REJECTED": "Closed",
    }),
    ("review_history", "new_status", {
        "PENDING": "InReview", "APPROVED": "Verified", "REJECTED": "Closed",
    }),
    ("wbs_items", "status", {
        "NOT_STARTED": "New", "IN_PROGRESS": "InProgress", "COMPLETED": "Verified",
        "BLOCKED": "ReworkRequired", "CANCELLED": "Closed",
    }),
    ("test_plans", "status", {
        "DRAFT": "New", "APPROVED": "Verified", "ACTIVE": "InProgress",
    }),
    ("test_milestones", "status", {
        "PLANNED": "New", "IN_PROGRESS": "InProgress",
        "COMPLETED": "Verified", "CANCELLED": "Closed",
    }),
    ("projects", "status", {
        "Planning": "New", "Active": "InProgress",
        "OnHold": "Assigned", "Archived": "Closed",
    }),
]


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    for table, column, remap in _REMAPS:
        if not inspector.has_table(table):
            continue
        # 確認欄位存在(避免 schema 漂移時爆)
        cols = {c["name"] for c in inspector.get_columns(table)}
        if column not in cols:
            continue
        for old, new in remap.items():
            op.execute(
                sa.text(
                    f'UPDATE {table} SET {column} = :new WHERE {column} = :old'
                ).bindparams(old=old, new=new)
            )


def downgrade() -> None:
    # no-op:同 0011/0012,不做反向 mapping
    pass
