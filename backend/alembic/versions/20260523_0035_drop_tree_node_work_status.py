"""drop tree_nodes.work_status(0032 加的孤兒欄位)

Revision ID: 0035_drop_tree_node_work_status
Revises: 0034_todo_start_date
Create Date: 2026-05-23

歷史:
0032 為 testcase 工作流 Kanban 加 ``tree_nodes.work_status``。後來測試看版
改用 TodoItem(commit 0f9a95d),test_kanban router 跟此欄位變孤兒 — 0 個
endpoint 讀,12 筆 tree_node row 全部停在 server_default 'NEW' 沒人改。
本次清理 drop 整個 column 跟對應 index。

注意:
- destructive 操作 — downgrade 可重建欄位但不能還原原始 value(本來就都是
  'NEW',實務上無損失)。
- testcase / report / 截圖 / 缺陷 / 待辦 / 審核 等資料完全不受影響(在不
  同表 / 不同欄位)。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0035_drop_tree_node_work_status"
down_revision: Union[str, None] = "0034_todo_start_date"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 用 IF EXISTS 防呆:萬一 0032 在某些環境沒跑成功,這條也不會炸
    op.execute("DROP INDEX IF EXISTS ix_tree_nodes_work_status")
    op.execute("ALTER TABLE tree_nodes DROP COLUMN IF EXISTS work_status")


def downgrade() -> None:
    op.add_column(
        "tree_nodes",
        sa.Column(
            "work_status",
            sa.String(length=20),
            nullable=False,
            server_default="NEW",
        ),
    )
    op.create_index("ix_tree_nodes_work_status", "tree_nodes", ["work_status"])
