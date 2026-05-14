"""execution_steps_log.created_at — for execution-order display in report

Revision ID: 0020_step_log_created_at
Revises: 0019_users_preferred_agent
Create Date: 2026-05-13

加 `execution_steps_log.created_at`(NOT NULL,default NOW()),讓 backend GET
`/api/reports/{id}/steps` 可以 `ORDER BY created_at, step_index` 呈現真實執行
順序。

背景:從 0018+ 的「per-testcase step attribution」之後,setup 跟 main 各自的
step_index 都從 0 開始 — 光 ORDER BY step_index 兩 case 的 step 會交錯,
前端 bucketing 時 OUTER case 順序變得 undefined(取決於 PK / 進場順序),
看起來像「沒有照執行順序排列」。

對既有 row:server_default NOW() 在 migration 跑當下會把所有 NULL 填 NOW(),
舊報告的所有 step 拿到相同 timestamp → 等於 fall back 到原本的 step_index
排序(這對「舊報告 cross-case 順序」沒有絕對保證,但 within-case 順序仍正確,
且舊報告通常已經被使用者看完不再care)。
"""
from typing import Union

from alembic import op
import sqlalchemy as sa


revision: str = "0020_step_log_created_at"
down_revision: Union[str, None] = "0019_users_preferred_agent"
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None


def _column_exists(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    return any(c["name"] == column_name for c in insp.get_columns(table_name))


def upgrade() -> None:
    if not _column_exists("execution_steps_log", "created_at"):
        op.add_column(
            "execution_steps_log",
            sa.Column(
                "created_at",
                sa.DateTime(),
                server_default=sa.text("NOW()"),
                nullable=False,
            ),
        )
        # 額外做一個 index 加速 ORDER BY(report_id + created_at 的常見組合)
        op.create_index(
            "ix_execution_steps_log_report_created",
            "execution_steps_log",
            ["report_id", "created_at"],
        )


def downgrade() -> None:
    if _column_exists("execution_steps_log", "created_at"):
        op.drop_index(
            "ix_execution_steps_log_report_created",
            table_name="execution_steps_log",
        )
        op.drop_column("execution_steps_log", "created_at")
