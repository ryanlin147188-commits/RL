"""project_role_permissions: per-project role permission override

Revision ID: 0026_project_role_permissions
Revises: 0025_recreate_pw_reset_tokens
Create Date: 2026-05-20

v1.1.6 新增「同一個 Role 在不同專案內可有不同 permissions」的能力。新表
``project_role_permissions``:

* 一個 ``(project_id, role_id)`` 一筆 row(UNIQUE)
* ``permissions_json`` = 該專案內覆蓋過的 permission key 清單
* 沒在表內 → 該專案內該 role 沿用 ``roles.permissions_json``(全域預設)

Casbin sync 看到 override 時會寫 alias role ``<role.name>@<short_pid>``
給該 (pid, role) 用,backend 端 ``require_casbin`` 不需動。
"""
from typing import Union

from alembic import op
import sqlalchemy as sa


revision: str = "0026_project_role_permissions"
down_revision: Union[str, None] = "0025_recreate_pw_reset_tokens"
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None


def _table_exists(name: str) -> bool:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    return name in insp.get_table_names()


def upgrade() -> None:
    if _table_exists("project_role_permissions"):
        return
    op.create_table(
        "project_role_permissions",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(length=36),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "role_id",
            sa.String(length=36),
            sa.ForeignKey("roles.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("permissions_json", sa.JSON(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(),
            nullable=False, server_default=sa.text("NOW()"),
        ),
        sa.Column(
            "updated_at", sa.DateTime(),
            nullable=False, server_default=sa.text("NOW()"),
        ),
        sa.UniqueConstraint(
            "project_id", "role_id", name="uq_project_role_permissions_pair",
        ),
    )


def downgrade() -> None:
    if _table_exists("project_role_permissions"):
        op.drop_table("project_role_permissions")
