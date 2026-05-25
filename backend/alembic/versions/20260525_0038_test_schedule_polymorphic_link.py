"""test_schedules 連結項目改成 polymorphic(linked_target_type + linked_target_id)

Revision ID: 0038_test_schedule_polymorphic_link
Revises: 0037_test_schedule_progress
Create Date: 2026-05-25

歷史:
原本 ``linked_test_round_id`` 只能連到 TestRound(有 FK 限制)。User 反映
這欄太窄,新增/編輯時程時應該能連到 testcase / report / defect / project
等其他 entity(跟 TodoItem 的連結項目一致)。

本次改成 polymorphic 連結:
* DROP FK constraint(跨多表 polymorphic 不適合 FK,完整性由 app 層保證)
* RENAME ``linked_test_round_id`` → ``linked_target_id``
* ADD ``linked_target_type`` 欄位(nullable,值如 'test_round' / 'testcase'
  / 'report' / 'defect' / 'project')
* 既有資料的 linked_target_id 不變,額外 set linked_target_type='test_round'

注意:
- ``linked_target_id`` 已索引(原 linked_test_round_id 有 index=True)
- 沒掛 FK 後刪 entity 不再自動清掉本欄,可能變孤兒(顯示時 frontend 要
  容錯)
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0038_ts_poly_link"
down_revision: Union[str, None] = "0037_test_schedule_progress"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1) 拆 FK constraint(若存在)
    op.execute("""
        DO $$
        DECLARE
            fk_name text;
        BEGIN
            SELECT conname INTO fk_name
            FROM pg_constraint
            WHERE conrelid = 'test_schedules'::regclass
              AND contype = 'f'
              AND pg_get_constraintdef(oid) ILIKE '%test_rounds%';
            IF fk_name IS NOT NULL THEN
                EXECUTE 'ALTER TABLE test_schedules DROP CONSTRAINT ' || quote_ident(fk_name);
            END IF;
        END $$;
    """)
    # 2) rename column
    op.alter_column(
        "test_schedules",
        "linked_test_round_id",
        new_column_name="linked_target_id",
    )
    # 3) 加 linked_target_type 欄位
    op.add_column(
        "test_schedules",
        sa.Column("linked_target_type", sa.String(length=32), nullable=True),
    )
    # 4) 現有 row 若有 linked_target_id,標 type='test_round'
    op.execute(
        "UPDATE test_schedules SET linked_target_type = 'test_round' "
        "WHERE linked_target_id IS NOT NULL"
    )


def downgrade() -> None:
    # 反向:把不是 test_round 的 link 清掉(只保留 test_round 才能 restore FK)
    op.execute(
        "UPDATE test_schedules SET linked_target_id = NULL "
        "WHERE linked_target_type IS NOT NULL AND linked_target_type <> 'test_round'"
    )
    op.drop_column("test_schedules", "linked_target_type")
    op.alter_column(
        "test_schedules",
        "linked_target_id",
        new_column_name="linked_test_round_id",
    )
    op.create_foreign_key(
        "test_schedules_linked_test_round_id_fkey",
        "test_schedules",
        "test_rounds",
        ["linked_test_round_id"],
        ["id"],
        ondelete="SET NULL",
    )
