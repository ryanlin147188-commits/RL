"""precondition + env binding -- testcase_precondition_links / testcase_env_bindings

Revision ID: 0015_precondition_env_binding
Revises: 0014_wbs_hierarchy
Create Date: 2026-05-07

兩件事:
  1. ``testcase_precondition_links`` — testcase 之間的「前置案例」單向 FK。
     一個 testcase 可掛多個 precondition,以 ``sort_order`` 排序;
     ``on_failure`` 預設 'stop'(前置失敗就中斷主案例)。
  2. ``testcase_env_bindings`` — testcase 綁定的 project env var name 清單。
     不存 env var id,因為 project_env_vars 是「整批替換」管理(整批 PUT
     會掉 row id),只記名字最穩。

審核者必填(``review_records.assigned_to``)在 API 層強制,**不**改 DB 層
nullable,以免歷史 row migration 出錯;新流程必填靠 API validator 擋。

idempotent:每個 op 都先 inspect 是否存在再建立。
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0015_precondition_env_binding"
down_revision: Union[str, None] = "0014_wbs_hierarchy"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def _column_exists(table: str, column: str) -> bool:
    bind = op.get_bind()
    return any(c["name"] == column for c in sa.inspect(bind).get_columns(table))


def upgrade() -> None:
    # 0) execution_reports.source_node_ids — local mode 多選時 agent 認領要重展開
    if _table_exists("execution_reports") and not _column_exists(
        "execution_reports", "source_node_ids"
    ):
        op.add_column(
            "execution_reports",
            sa.Column("source_node_ids", sa.JSON(), nullable=True),
        )

    if not _table_exists("testcase_precondition_links"):
        op.create_table(
            "testcase_precondition_links",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "organization_id",
                sa.String(36),
                sa.ForeignKey("organizations.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column(
                "testcase_id",
                sa.String(36),
                sa.ForeignKey("tree_nodes.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "precondition_testcase_id",
                sa.String(36),
                sa.ForeignKey("tree_nodes.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
            sa.Column(
                "enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.true(),
            ),
            sa.Column(
                "on_failure",
                sa.String(20),
                nullable=False,
                server_default="stop",
            ),
            sa.Column("created_by", sa.String(80), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.UniqueConstraint(
                "testcase_id",
                "precondition_testcase_id",
                name="uq_precondition_pair",
            ),
        )
        op.create_index(
            "ix_precondition_testcase_id",
            "testcase_precondition_links",
            ["testcase_id"],
        )
        op.create_index(
            "ix_precondition_target_id",
            "testcase_precondition_links",
            ["precondition_testcase_id"],
        )

    if not _table_exists("testcase_env_bindings"):
        op.create_table(
            "testcase_env_bindings",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "organization_id",
                sa.String(36),
                sa.ForeignKey("organizations.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column(
                "testcase_id",
                sa.String(36),
                sa.ForeignKey("tree_nodes.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("env_var_name", sa.String(120), nullable=False),
            sa.Column("created_by", sa.String(80), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.UniqueConstraint(
                "testcase_id",
                "env_var_name",
                name="uq_env_binding_pair",
            ),
        )
        op.create_index(
            "ix_env_binding_testcase_id",
            "testcase_env_bindings",
            ["testcase_id"],
        )


def downgrade() -> None:
    if _table_exists("execution_reports") and _column_exists(
        "execution_reports", "source_node_ids"
    ):
        op.drop_column("execution_reports", "source_node_ids")
    # Postgres 在 DROP TABLE 時會自動回收依附在該表的 index;不需要先手動 drop_index
    # (而且早期版本 alembic 的 op.drop_index 沒有 if_exists 參數)。
    if _table_exists("testcase_env_bindings"):
        op.drop_table("testcase_env_bindings")
    if _table_exists("testcase_precondition_links"):
        op.drop_table("testcase_precondition_links")
