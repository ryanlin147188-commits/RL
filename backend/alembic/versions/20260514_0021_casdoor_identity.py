"""users.casdoor_user_id + users.token_generation — Casdoor 身分整合

Revision ID: 0021_casdoor_identity
Revises: 0020_step_log_created_at
Create Date: 2026-05-14

Phase 1 of the Casdoor + Casbin migration plan.

* ``casdoor_user_id``(VARCHAR(255), nullable, indexed):當使用者透過 Casdoor
  完成 OIDC 登入後,backend 把 Casdoor 的 ``sub`` claim(uuid string)寫到這欄。
  webhook / 5-min reconcile job 用這欄做 join(舊資料 username 跟 Casdoor
  display name 可能不一致,uuid 才是 stable identity)。
* ``token_generation``(INTEGER, NOT NULL, default 0):強制讓在飛的 HS256
  舊 token 失效用。Phase 4 cutover 時把全表 +1,middleware 看到 payload 中
  ``gen`` < 當前 user.token_generation 直接 401。預設 0 表示「不檢查」
  (Phase 1/2 還沒接到 middleware 之前完全等價於不存在這欄)。

兩欄都不能 NOT NULL casdoor_user_id — 因為(a)既有 user 還沒做 OIDC 註冊
(b)有些 service account 不會走 Casdoor。
"""
from typing import Union

from alembic import op
import sqlalchemy as sa


revision: str = "0021_casdoor_identity"
down_revision: Union[str, None] = "0020_step_log_created_at"
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None


def _column_exists(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    return any(c["name"] == column_name for c in insp.get_columns(table_name))


def _index_exists(table_name: str, index_name: str) -> bool:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    return any(idx["name"] == index_name for idx in insp.get_indexes(table_name))


def upgrade() -> None:
    if not _column_exists("users", "casdoor_user_id"):
        op.add_column(
            "users",
            sa.Column("casdoor_user_id", sa.String(length=255), nullable=True),
        )
    if not _index_exists("users", "ix_users_casdoor_user_id"):
        op.create_index(
            "ix_users_casdoor_user_id",
            "users",
            ["casdoor_user_id"],
            unique=True,
            postgresql_where=sa.text("casdoor_user_id IS NOT NULL"),
        )
    if not _column_exists("users", "token_generation"):
        op.add_column(
            "users",
            sa.Column(
                "token_generation",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            ),
        )


def downgrade() -> None:
    if _index_exists("users", "ix_users_casdoor_user_id"):
        op.drop_index("ix_users_casdoor_user_id", table_name="users")
    if _column_exists("users", "casdoor_user_id"):
        op.drop_column("users", "casdoor_user_id")
    if _column_exists("users", "token_generation"):
        op.drop_column("users", "token_generation")
