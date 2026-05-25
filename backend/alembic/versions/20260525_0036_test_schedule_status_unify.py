"""test_schedules.status 與測試看版(TodoStatus)統一為 5 值

Revision ID: 0036_test_schedule_status_unify
Revises: 0035_drop_tree_node_work_status
Create Date: 2026-05-25

歷史:
v1.1.9 first cut 用 Planned / InProgress / Done / Delayed / Cancelled,但跟
測試看版的 TodoStatus(Todo / InProgress / InReview / Verified / Closed)
不一致——首頁行事曆同時呈現「待辦 due」跟「時程 due」時,user 看到兩套
不同名詞會困惑。本次統一成跟測試看版同一組值。

Mapping:
    Planned   → Todo
    InProgress→ InProgress(同名,不動)
    Done      → Verified
    Delayed   → Todo  (無「延期」概念;統一當待辦,user 自己看開始/結束日)
    Cancelled → Closed

注意:
- ``test_schedules.status`` column 為 VARCHAR(20)(``native_enum=False``),
  schema 不必動,只需要 UPDATE rows。
- Downgrade 把新值轉回 Planned(無法精確還原 Delayed/Cancelled,user 必須
  事後手動標)。
"""
from typing import Sequence, Union

from alembic import op


revision: str = "0036_test_schedule_status_unify"
down_revision: Union[str, None] = "0035_drop_tree_node_work_status"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("UPDATE test_schedules SET status = 'Todo'     WHERE status = 'Planned'")
    op.execute("UPDATE test_schedules SET status = 'Todo'     WHERE status = 'Delayed'")
    op.execute("UPDATE test_schedules SET status = 'Verified' WHERE status = 'Done'")
    op.execute("UPDATE test_schedules SET status = 'Closed'   WHERE status = 'Cancelled'")


def downgrade() -> None:
    # Verified / Todo / Closed 都可能來自不同舊值;保守一律降回 Planned。
    op.execute("UPDATE test_schedules SET status = 'Planned' WHERE status IN ('Todo','InReview','Verified','Closed')")
