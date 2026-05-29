"""skills + agent_sessions.active_skill_id — Phase 2a Skill 系統

Revision ID: 0052_skills
Revises: 0051_agent_session_memory
Create Date: 2026-05-29

Skill = per-org 工作流模板;啟用後 append system prompt + 限縮 tool 白名單。
agent_sessions 加 active_skill_id FK(nullable + ondelete SET NULL),既有
session 不受影響。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0052_skills"
down_revision: Union[str, None] = "0051_agent_session_memory"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "skills",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("organization_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("trigger_keywords", sa.JSON(), nullable=False),
        sa.Column(
            "system_prompt_addition", sa.Text(), nullable=False, server_default=""
        ),
        sa.Column("allowed_tools", sa.JSON(), nullable=True),
        sa.Column("mode_scope", sa.JSON(), nullable=False),
        sa.Column(
            "enabled", sa.Boolean(), nullable=False, server_default="true"
        ),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_by", sa.String(length=36), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], ondelete="SET NULL"
        ),
        sa.UniqueConstraint(
            "organization_id", "name", name="uq_skills_org_name"
        ),
    )
    op.create_index("ix_skills_org", "skills", ["organization_id"])
    op.create_index("ix_skills_name", "skills", ["name"])

    op.add_column(
        "agent_sessions",
        sa.Column("active_skill_id", sa.String(length=36), nullable=True),
    )
    op.create_index(
        "ix_agent_sessions_active_skill_id",
        "agent_sessions",
        ["active_skill_id"],
    )
    op.create_foreign_key(
        "fk_agent_sessions_active_skill_id",
        "agent_sessions",
        "skills",
        ["active_skill_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_agent_sessions_active_skill_id",
        "agent_sessions",
        type_="foreignkey",
    )
    op.drop_index(
        "ix_agent_sessions_active_skill_id", table_name="agent_sessions"
    )
    op.drop_column("agent_sessions", "active_skill_id")

    op.drop_index("ix_skills_name", table_name="skills")
    op.drop_index("ix_skills_org", table_name="skills")
    op.drop_table("skills")
