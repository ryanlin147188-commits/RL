"""test_schedules 加 progress 欄位(0-100)

Revision ID: 0037_test_schedule_progress
Revises: 0036_test_schedule_status_unify
Create Date: 2026-05-25

歷史:
v1.1.9 後測試時程改用 Gantt-style「規劃工具」介面(類專案管理工具),
新增「進度百分比」欄位讓 user 標每個階段 / 里程碑的完成度,獨立於
status(status 是離散狀態,progress 是線性 0-100 百分比)。

預設值 0(未開始)。舊資料一律 0,user 可在 modal 內手動拉 slider 改。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0037_test_schedule_progress"
down_revision: Union[str, None] = "0036_test_schedule_status_unify"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "test_schedules",
        sa.Column("progress", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("test_schedules", "progress")
