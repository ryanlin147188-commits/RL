"""users: rename casdoor_user_id → oidc_subject + add oidc_provider

Revision ID: 0024_rename_oidc_columns
Revises: 0023_drop_password_reset_tokens
Create Date: 2026-05-18

v1.1.5 把 IAM 從 Casdoor 切回 in-process authlib(只跟 Zoho / Google / 任意
OIDC IdP 對接,不再走 Casdoor 中介)。原本 ``users.casdoor_user_id`` 語意
過窄,改成 generic ``oidc_subject`` 同時新增 ``oidc_provider`` 欄位,讓
(provider, subject) 兩欄一起當 stable identity:

    | oidc_provider | oidc_subject  | 用途                                |
    |---------------|---------------|-------------------------------------|
    | NULL          | NULL          | 純本地密碼帳號(管理員建,bcrypt)   |
    | 'zoho'        | '<Zoho ZUID>' | 一次走過 Zoho SSO 的使用者          |
    | 'google'      | '<Google sub>'| (未來啟用 Google 時)               |

Partial unique index 改成 ``(oidc_provider, oidc_subject)`` 一組唯一,允許
跨 provider 重複 subject(雖然極端情況才會發生)。
"""
from typing import Union

from alembic import op
import sqlalchemy as sa


revision: str = "0024_rename_oidc_columns"
down_revision: Union[str, None] = "0023_drop_password_reset_tokens"
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
    # 1) drop 舊的 ``ix_users_casdoor_user_id`` partial unique index(0021 建的)
    if _index_exists("users", "ix_users_casdoor_user_id"):
        op.drop_index("ix_users_casdoor_user_id", table_name="users")

    # 2) rename casdoor_user_id → oidc_subject(資料保留;Casdoor cutover
    #    期間寫進去的 sub 雖然不再對得到任何 IdP,留著也無害,反正下次該
    #    使用者走 Zoho 登入會被 ``email`` fallback 重新綁定)
    if _column_exists("users", "casdoor_user_id") and not _column_exists("users", "oidc_subject"):
        op.alter_column("users", "casdoor_user_id", new_column_name="oidc_subject")

    # 3) 新增 oidc_provider(nullable,沒走過 SSO 的列就是 NULL)
    if not _column_exists("users", "oidc_provider"):
        op.add_column(
            "users",
            sa.Column("oidc_provider", sa.String(length=40), nullable=True),
        )

    # 4) 新 partial unique index:(provider, subject) 同時 NOT NULL 時 unique
    if not _index_exists("users", "ix_users_oidc_provider_subject"):
        op.create_index(
            "ix_users_oidc_provider_subject",
            "users",
            ["oidc_provider", "oidc_subject"],
            unique=True,
            postgresql_where=sa.text("oidc_provider IS NOT NULL AND oidc_subject IS NOT NULL"),
        )


def downgrade() -> None:
    if _index_exists("users", "ix_users_oidc_provider_subject"):
        op.drop_index("ix_users_oidc_provider_subject", table_name="users")
    if _column_exists("users", "oidc_provider"):
        op.drop_column("users", "oidc_provider")
    if _column_exists("users", "oidc_subject") and not _column_exists("users", "casdoor_user_id"):
        op.alter_column("users", "oidc_subject", new_column_name="casdoor_user_id")
    if not _index_exists("users", "ix_users_casdoor_user_id"):
        op.create_index(
            "ix_users_casdoor_user_id",
            "users",
            ["casdoor_user_id"],
            unique=True,
            postgresql_where=sa.text("casdoor_user_id IS NOT NULL"),
        )
