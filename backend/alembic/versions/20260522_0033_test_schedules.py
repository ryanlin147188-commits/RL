"""test_schedules:測試時程 / 規劃里程碑表

Revision ID: 0033_test_schedules
Revises: 0032_tree_node_work_status
Create Date: 2026-05-22

新增 ``test_schedules`` 表,讓 user 在「測試時程」分頁規劃 Sprint /
階段 / 里程碑(start_date / end_date / status / color),supplement 既
有 ``schedules``(cron-style 觸發測試)。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0033_test_schedules"
down_revision: Union[str, None] = "0032_tree_node_work_status"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "test_schedules",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("project_id", sa.String(length=36), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("start_date", sa.Date, nullable=False),
        sa.Column("end_date", sa.Date, nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="Planned"),
        sa.Column("color", sa.String(length=20), nullable=False, server_default="blue"),
        sa.Column("linked_test_round_id", sa.String(length=36), sa.ForeignKey("test_rounds.id", ondelete="SET NULL"), nullable=True, index=True),
        # Assignable mixin 欄位
        sa.Column("assigned_to", sa.String(length=80), nullable=True, index=True),
        sa.Column("assigned_to_type", sa.String(length=10), nullable=False, server_default="user"),
        sa.Column("assigned_by", sa.String(length=80), nullable=True),
        sa.Column("assigned_at", sa.DateTime, nullable=True),
        # TenantScoped
        sa.Column("organization_id", sa.String(length=36), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=True, index=True),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("test_schedules")
