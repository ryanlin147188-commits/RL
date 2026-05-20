"""project_invites: pull-based 加入專案邀請流程

Revision ID: 0030_project_invites
Revises: 0029_users_pk_to_uuid
Create Date: 2026-05-22

Pull-based onboarding flow:
- admin POST /api/projects/{pid}/invites { invitee_email, role_id } → 產生
  invite_code + 寫 row + 寄信
- invitee 登入後 POST /api/projects/invites/redeem { invite_code } → 驗證
  email 完全相符 + 未過期 + 未兌換 → 建 ProjectMember + 標 redeemed
"""
from alembic import op
import sqlalchemy as sa


revision = "0030_project_invites"
down_revision = "0029_users_pk_to_uuid"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "project_invites",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(36),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("organization_id", sa.String(36), nullable=False),
        sa.Column("invitee_email", sa.String(255), nullable=False),
        sa.Column(
            "role_id",
            sa.String(36),
            sa.ForeignKey("roles.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("invite_code", sa.String(32), nullable=False),
        sa.Column("inviter_username", sa.String(64), nullable=False),
        sa.Column(
            "status",
            sa.String(16),
            nullable=False,
            server_default="pending",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("redeemed_at", sa.DateTime(), nullable=True),
        sa.Column("redeemed_by_username", sa.String(64), nullable=True),
    )
    op.create_index(
        "ix_project_invites_code", "project_invites", ["invite_code"], unique=True
    )
    op.create_index(
        "ix_project_invites_email", "project_invites", ["invitee_email"]
    )
    op.create_index(
        "ix_project_invites_proj", "project_invites", ["project_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_project_invites_proj", table_name="project_invites")
    op.drop_index("ix_project_invites_email", table_name="project_invites")
    op.drop_index("ix_project_invites_code", table_name="project_invites")
    op.drop_table("project_invites")
