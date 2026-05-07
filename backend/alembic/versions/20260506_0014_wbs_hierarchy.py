"""wbs_hierarchy -- WBS v1 階層 + Task 連結

Revision ID: 0014_wbs_hierarchy
Revises: 0013_fix_legacy_status_names
Create Date: 2026-05-06

兩件事:
  1. 給 wbs_items 加 ``item_type`` 欄位(VARCHAR(20),default 'Task')
     讓現有 WBS 進入「Feature → WorkPackage → Task」三層階層。既有資料一律
     視為 Task(最常見也最沒副作用)。
  2. 新表 ``wbs_links`` — Task 葉節點的 M:N 連結表,target 限定:
     todo / testcase / defect / execution_report。

idempotent:
  * 兩個 op 都先 inspect 是否已存在再建立。
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0014_wbs_hierarchy"
down_revision: Union[str, None] = "0013_fix_legacy_status_names"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def _column_exists(table: str, column: str) -> bool:
    bind = op.get_bind()
    return any(c["name"] == column for c in sa.inspect(bind).get_columns(table))


def upgrade() -> None:
    # 1) wbs_items.item_type
    if _table_exists("wbs_items") and not _column_exists("wbs_items", "item_type"):
        op.add_column(
            "wbs_items",
            sa.Column(
                "item_type",
                sa.String(20),
                nullable=False,
                server_default="Task",
            ),
        )
        op.create_index("ix_wbs_items_item_type", "wbs_items", ["item_type"])

    # 2) wbs_links 表
    if not _table_exists("wbs_links"):
        op.create_table(
            "wbs_links",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "organization_id",
                sa.String(36),
                sa.ForeignKey("organizations.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column(
                "wbs_item_id",
                sa.String(36),
                sa.ForeignKey("wbs_items.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("target_type", sa.String(40), nullable=False),
            sa.Column("target_id", sa.String(36), nullable=False),
            sa.Column("created_by", sa.String(80), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.UniqueConstraint(
                "wbs_item_id", "target_type", "target_id",
                name="uq_wbs_link_triple",
            ),
        )
        op.create_index("ix_wbs_links_org", "wbs_links", ["organization_id"])
        op.create_index("ix_wbs_links_wbs_item", "wbs_links", ["wbs_item_id"])


def downgrade() -> None:
    # DROP TABLE 在 Postgres 會 cascade 把 index 一起回收;不必先手動 drop_index
    # (有些上線時 index 沒被成功 create,顯式 drop 會炸 UndefinedObject)。
    if _table_exists("wbs_links"):
        op.drop_table("wbs_links")
    if _table_exists("wbs_items") and _column_exists("wbs_items", "item_type"):
        op.drop_index(
            "ix_wbs_items_item_type", table_name="wbs_items", if_exists=True
        )
        op.drop_column("wbs_items", "item_type")
