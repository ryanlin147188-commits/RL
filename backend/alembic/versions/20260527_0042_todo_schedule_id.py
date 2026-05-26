"""todo_items.schedule_id — 看板任務歸屬 Sprint(test_schedules)

Revision ID: 0042_todo_schedule_id
Revises: 0041_sprint_links
Create Date: 2026-05-27

歷史:
v1.1.11.2 把 Sprint(test_schedules)跟看板任務(todo_items)的關係從
「Sprint 端連結多筆」反轉成「看板任務歸屬 1 個 Sprint」。新增待辦 modal
加 Sprint dropdown 寫入這個 FK。

設計:
- ``schedule_id`` FK ON DELETE SET NULL — 刪 Sprint 不要連帶殺 todo
- nullable=True — 沒指派 Sprint 仍合法(對應「Backlog」狀態)
- 既有 ``sprint_label`` 純文字欄位保留為唯讀,新功能走 schedule_id
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0042_todo_schedule_id"
down_revision: Union[str, None] = "0041_sprint_links"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "todo_items",
        sa.Column("schedule_id", sa.String(length=36), nullable=True),
    )
    op.create_index("ix_todo_items_schedule_id", "todo_items", ["schedule_id"])
    op.create_foreign_key(
        "fk_todo_items_schedule_id_test_schedules",
        "todo_items", "test_schedules",
        ["schedule_id"], ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_todo_items_schedule_id_test_schedules", "todo_items", type_="foreignkey")
    op.drop_index("ix_todo_items_schedule_id", table_name="todo_items")
    op.drop_column("todo_items", "schedule_id")
