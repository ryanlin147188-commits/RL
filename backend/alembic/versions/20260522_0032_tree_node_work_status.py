"""tree_nodes 加 work_status 欄位

Revision ID: 0032_tree_node_work_status
Revises: 0031_review_entity_defect
Create Date: 2026-05-22

v1.1.9:測試看板 Kanban 把 testcase 拖拽分到 5 個工作流狀態
(NEW / IN_PROGRESS / PASSED / FAILED / RETEST),要在 tree_nodes 加一個
``work_status`` 欄位儲存。default NEW,既有資料 server_default 自動填。

不用 PG enum 是怕未來新增/移除狀態又要走 ALTER TYPE,改 String(20) + 預期
應用層 enforce 範圍即可。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0032_tree_node_work_status"
down_revision: Union[str, None] = "0031_review_entity_defect"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "tree_nodes",
        sa.Column(
            "work_status",
            sa.String(length=20),
            nullable=False,
            server_default="NEW",
        ),
    )
    op.create_index(
        "ix_tree_nodes_work_status",
        "tree_nodes",
        ["work_status"],
    )


def downgrade() -> None:
    op.drop_index("ix_tree_nodes_work_status", table_name="tree_nodes")
    op.drop_column("tree_nodes", "work_status")
