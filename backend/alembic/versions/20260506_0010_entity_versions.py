"""entity_versions table + content_status on 6 entity tables

Revision ID: 0010_entity_versions
Revises: 0009_password_reset_tokens
Create Date: 2026-05-06

實作「AB 表設計 / AI 生成 / 審核前後 / 任意還原」的 schema 基礎:

  1. 新表 ``entity_versions`` — 通用 polymorphic 快照表;每個 entity 的
     每次寫入(human / ai / system / revert)都 mirror 一筆完整 JSON 進去。
  2. 給 6 張業務主表加 ``content_status`` 欄(``ai_draft`` / ``pending_review``
     / ``approved`` / ``rejected``):tree_nodes / defects / requirements /
     test_documents / wbs_items / todo_items。default ``approved`` 讓既有
     資料視為已上線版,不會被審核 gate 擋住。

idempotent:每個 op 都先檢查是否已存在,讓 fresh DB(0001 baseline 已 create_all)
跟既有 DB 都能順利推上來。
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0010_entity_versions"
down_revision: Union[str, None] = "0009_password_reset_tokens"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def _column_exists(table: str, column: str) -> bool:
    bind = op.get_bind()
    return any(c["name"] == column for c in sa.inspect(bind).get_columns(table))


_STATUS_TABLES = (
    "tree_nodes",
    "defects",
    "requirements",
    "test_documents",
    "wbs_items",
    "todo_items",
)


def upgrade() -> None:
    # ── 1) 新表 entity_versions ────────────────────────────────────
    if not _table_exists("entity_versions"):
        op.create_table(
            "entity_versions",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("entity_type", sa.String(40), nullable=False),
            sa.Column("entity_id", sa.String(36), nullable=False),
            sa.Column("version_no", sa.Integer(), nullable=False),
            sa.Column("content_snapshot", sa.dialects.postgresql.JSONB(), nullable=False),
            sa.Column("content_status", sa.String(20), nullable=False),
            sa.Column("change_source", sa.String(20), nullable=False),
            sa.Column("changed_by", sa.String(80), nullable=True),
            sa.Column("change_reason", sa.Text(), nullable=True),
            sa.Column(
                "parent_version_id",
                sa.String(36),
                sa.ForeignKey("entity_versions.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column(
                "organization_id",
                sa.String(36),
                sa.ForeignKey("organizations.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        )
        op.create_index("ix_entity_versions_lookup", "entity_versions", ["entity_type", "entity_id"])
        op.create_index("ix_entity_versions_org", "entity_versions", ["organization_id"])

    # ── 2) 6 張主表加 content_status 欄 ───────────────────────────
    for tbl in _STATUS_TABLES:
        if not _table_exists(tbl):
            continue  # fresh DB 可能還沒推到 baseline
        if not _column_exists(tbl, "content_status"):
            op.add_column(
                tbl,
                sa.Column(
                    "content_status",
                    sa.String(20),
                    nullable=False,
                    server_default="approved",
                ),
            )
            op.create_index(
                f"ix_{tbl}_content_status", tbl, ["content_status"]
            )


def downgrade() -> None:
    for tbl in _STATUS_TABLES:
        if _table_exists(tbl) and _column_exists(tbl, "content_status"):
            op.drop_index(f"ix_{tbl}_content_status", table_name=tbl)
            op.drop_column(tbl, "content_status")
    if _table_exists("entity_versions"):
        op.drop_index("ix_entity_versions_org", table_name="entity_versions")
        op.drop_index("ix_entity_versions_lookup", table_name="entity_versions")
        op.drop_table("entity_versions")
