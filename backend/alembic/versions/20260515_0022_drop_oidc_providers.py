"""drop oidc_providers — Casdoor 接管 SSO 聯邦

Revision ID: 0022_drop_oidc_providers
Revises: 0021_casdoor_identity
Create Date: 2026-05-15

Phase 4.2 of the Casdoor + Casbin migration plan. 把 `oidc_providers` 整張表
drop 掉 — Casdoor 接管所有 OIDC / OAuth / SAML 聯邦設定,既有的 row 透過
``scripts/migrate-users-to-casdoor.py`` 在執行 migration 之前就先匯入 Casdoor。

執行順序(operator 必讀):

1. ``docker compose exec backend python scripts/migrate-users-to-casdoor.py``
   先 dry-run 看清楚要建立的 user / provider,確認後拿掉 ``--dry-run`` 再執行一次。
2. 在 Casdoor admin UI 確認所有 provider 都建好且 enable。
3. 再跑 ``alembic upgrade head`` → 此 migration 觸發。

Downgrade 還原會把表結構建回來但 **資料拿不回來** — 是 destructive migration,
operator 要先在 PG 做 ``pg_dump --table=oidc_providers`` 備份才安心。
"""
from typing import Union

from alembic import op
import sqlalchemy as sa


revision: str = "0022_drop_oidc_providers"
down_revision: Union[str, None] = "0021_casdoor_identity"
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None


def _table_exists(name: str) -> bool:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    return name in insp.get_table_names()


def upgrade() -> None:
    if not _table_exists("oidc_providers"):
        return
    op.drop_table("oidc_providers")


def downgrade() -> None:
    if _table_exists("oidc_providers"):
        return
    # 跟 app.models.oidc_provider 的欄位定義對齊;data 拿不回來只能還原 schema。
    op.create_table(
        "oidc_providers",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "organization_id",
            sa.String(length=36),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=True,
            index=True,
        ),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("slug", sa.String(length=40), nullable=False),
        sa.Column("discovery_url", sa.String(length=500), nullable=True),
        sa.Column("issuer", sa.String(length=500), nullable=True),
        sa.Column("authorize_url", sa.String(length=500), nullable=True),
        sa.Column("token_url", sa.String(length=500), nullable=True),
        sa.Column("jwks_url", sa.String(length=500), nullable=True),
        sa.Column("client_id", sa.String(length=255), nullable=False),
        sa.Column("client_secret", sa.String(length=800), nullable=True),
        sa.Column(
            "scopes", sa.String(length=300),
            nullable=False, server_default="openid email profile",
        ),
        sa.Column(
            "button_icon", sa.String(length=80),
            nullable=True, server_default="fa-solid fa-key",
        ),
        sa.Column("button_label", sa.String(length=80), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "created_at", sa.DateTime(),
            nullable=False, server_default=sa.text("NOW()"),
        ),
        sa.Column(
            "updated_at", sa.DateTime(),
            nullable=False, server_default=sa.text("NOW()"),
        ),
    )
