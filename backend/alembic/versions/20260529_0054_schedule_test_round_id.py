"""schedules.test_round_id — 排程支援綁定 TestRun(live link)

Revision ID: 0054_schedule_test_round_id
Revises: 0053_mcp_servers
Create Date: 2026-05-29

排程觸發時若有 test_round_id,以該 TestRun 當下的 node_ids_json 為準,
讓 TestRun 之後修改 testcase 集合會自動反映到後續排程觸發。
ondelete=SET NULL:刪掉 TestRun 時把排程退回「無綁定」(下次觸發走原本
schedule.node_ids_json / node_id),不連帶刪掉排程。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0054_schedule_test_round_id"
down_revision: Union[str, None] = "0053_mcp_servers"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "schedules",
        sa.Column("test_round_id", sa.String(length=36), nullable=True),
    )
    op.create_index(
        "ix_schedules_test_round_id", "schedules", ["test_round_id"]
    )
    op.create_foreign_key(
        "fk_schedules_test_round_id",
        "schedules",
        "test_rounds",
        ["test_round_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_schedules_test_round_id", "schedules", type_="foreignkey"
    )
    op.drop_index("ix_schedules_test_round_id", table_name="schedules")
    op.drop_column("schedules", "test_round_id")
