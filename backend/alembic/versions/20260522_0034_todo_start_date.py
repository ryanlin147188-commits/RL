"""todo_items 加 start_date 欄位

Revision ID: 0034_todo_start_date
Revises: 0033_test_schedules
Create Date: 2026-05-22

v1.1.9 起,新增待辦 modal 可填開始 + 結束兩個日期(原本只 due_date),
測試時程 Gantt 也會把有 start_date+due_date 的 todo 一起畫出來。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0034_todo_start_date"
down_revision: Union[str, None] = "0033_test_schedules"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "todo_items",
        sa.Column("start_date", sa.String(length=20), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("todo_items", "start_date")
