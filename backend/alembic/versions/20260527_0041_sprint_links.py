"""sprint_links 表 — Sprint(TestSchedule)跨實體 M:N 連結

Revision ID: 0041_sprint_links
Revises: 0040_email_verification
Create Date: 2026-05-27

歷史:
v1.1.11 把「測試時程」改名為「Sprint」並支援多項目連結;舊有
``test_schedules.linked_target_type/id`` 是 N:1 polymorphic 不夠用(一個
Sprint 常涵蓋多個 testcase + defect + 看板任務)。新表 sprint_links 提供
N:M;舊欄位保留為唯讀 legacy(GET /links 時 stitch 進來)直到清理。

設計:
- ``schedule_id`` FK CASCADE — 刪 Sprint 拉著清連結
- ``organization_id`` FK SET NULL — multi-tenant 隔離,org 刪掉不影響連結資料
- UniqueConstraint(schedule_id, target_type, target_id, link_kind) — 防重複連結
- ``target_type`` / ``target_id`` 不掛 FK(跨多表 polymorphic),完整性由 app 層保證
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0041_sprint_links"
down_revision: Union[str, None] = "0040_email_verification"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "sprint_links",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("organization_id", sa.String(length=36), nullable=True),
        sa.Column("schedule_id", sa.String(length=36), nullable=False),
        sa.Column("target_type", sa.String(length=40), nullable=False),
        sa.Column("target_id", sa.String(length=36), nullable=False),
        sa.Column("link_kind", sa.String(length=40), nullable=False, server_default="relates_to"),
        sa.Column("note", sa.String(length=300), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("created_by", sa.String(length=100), nullable=True),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["schedule_id"], ["test_schedules.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "schedule_id", "target_type", "target_id", "link_kind",
            name="uq_sprint_link_quad",
        ),
    )
    op.create_index("ix_sprint_links_organization_id", "sprint_links", ["organization_id"])
    op.create_index("ix_sprint_links_schedule_id", "sprint_links", ["schedule_id"])


def downgrade() -> None:
    op.drop_index("ix_sprint_links_schedule_id", table_name="sprint_links")
    op.drop_index("ix_sprint_links_organization_id", table_name="sprint_links")
    op.drop_table("sprint_links")
