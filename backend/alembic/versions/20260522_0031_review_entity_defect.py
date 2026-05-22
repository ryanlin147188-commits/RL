"""reviewable_entity_type 加 DEFECT enum 值

Revision ID: 0031_review_entity_defect
Revises: 0030_project_invites
Create Date: 2026-05-22

v1.1.9 起 Defect 也能進審核流程(autocreate listener 在 INSERT 時 spawn
ReviewRecord),但原 PG enum ``reviewable_entity_type`` 只認 TESTCASE /
DOCUMENT / SCRIPT / REPORT 四值。沒這個 migration,backfill + 新 defect
都會炸 ``InvalidTextRepresentationError: invalid input value for enum``。

implementation:
``ALTER TYPE ... ADD VALUE IF NOT EXISTS`` — PG 12+ 的標準寫法,idempotent。
注意:這條 statement 必須跑在 transaction 外面(PG 限制),所以用 op.execute
+ AUTOCOMMIT。
"""
from typing import Sequence, Union

from alembic import op


revision: str = "0031_review_entity_defect"
down_revision: Union[str, None] = "0030_project_invites"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ALTER TYPE ADD VALUE 在 PG 12+ 後可以在 transaction 內跑(雖然官方還是
    # 建議 outside),配合 IF NOT EXISTS 完全 idempotent。
    # Python enum 的 .name(大寫)是 SQLAlchemy 預設寫進 PG 的值,跟既有的
    # 'TESTCASE' / 'SCRIPT' / 'REPORT' 一致,所以新加的也是 'DEFECT' 大寫。
    op.execute("ALTER TYPE reviewable_entity_type ADD VALUE IF NOT EXISTS 'DEFECT'")


def downgrade() -> None:
    # PG 不支援 ALTER TYPE DROP VALUE。要 downgrade 必須:
    # 1. DROP TYPE + CREATE TYPE 去重新建,過程中需先 ALTER 所有 column 改 text
    # 2. 重灌 review_records 資料
    # 風險太高,且 v1.1.9 後沒人會想退回。故留 no-op。
    pass
