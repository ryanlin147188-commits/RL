"""multi_tenant_assignment -- per-user multi-org + per-project membership + role scope

Revision ID: 0006_multi_tenant_assignment
Revises: 0005_lightweight_sql
Create Date: 2026-05-04

Adds three things needed for "user belongs to multiple orgs + per-project
membership with per-project roles":

1. ``org_memberships`` table -- the explicit (user, org, role) tuple
   that lets a user belong to many orgs. ``users.organization_id``
   stays around as the *currently active* org snapshot (and the source
   of all tenant-scoped query filters).

2. ``project_members`` table -- the explicit (project, user, role)
   tuple that gates which users can see / act on which projects.
   Replaces the previous implicit "same-org users see all projects" rule.

3. ``roles.scope`` column -- distinguishes org-scoped roles (the
   existing 22 RBAC keys, applied across all projects in an org) from
   project-scoped roles (override of the org role for a specific project).

Backfill philosophy: zero regression. Every existing user gets an
OrgMembership for their current org (preserving role_id), and every
existing user × project (same org) gets a ProjectMember with the
existing org-level role_id. Behaviour is identical until an admin
actively starts limiting members.

Note on user FK: ``users.username`` is the PK (no UUID id column),
so all FKs to users use ``username`` -- same pattern as
``group_memberships``.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0006_multi_tenant_assignment"
down_revision: Union[str, None] = "0005_lightweight_sql"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── 1) org_memberships ────────────────────────────────────────────────
    op.create_table(
        "org_memberships",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "username",
            sa.String(80),
            sa.ForeignKey("users.username", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "organization_id",
            sa.String(36),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "role_id",
            sa.String(36),
            sa.ForeignKey("roles.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("joined_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column(
            "invited_by",
            sa.String(80),
            sa.ForeignKey("users.username", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.UniqueConstraint("username", "organization_id", name="uq_org_memberships_user_org"),
    )
    op.create_index("ix_org_memberships_username", "org_memberships", ["username"])
    op.create_index("ix_org_memberships_org", "org_memberships", ["organization_id"])

    # ── 2) project_members ────────────────────────────────────────────────
    op.create_table(
        "project_members",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(36),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "username",
            sa.String(80),
            sa.ForeignKey("users.username", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "role_id",
            sa.String(36),
            sa.ForeignKey("roles.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("joined_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column(
            "invited_by",
            sa.String(80),
            sa.ForeignKey("users.username", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.UniqueConstraint("project_id", "username", name="uq_project_members_project_user"),
    )
    op.create_index("ix_project_members_project", "project_members", ["project_id"])
    op.create_index("ix_project_members_username", "project_members", ["username"])

    # ── 3) roles.scope ────────────────────────────────────────────────────
    op.add_column(
        "roles",
        sa.Column("scope", sa.String(16), nullable=False, server_default="org"),
    )

    # ── 4) Backfill ───────────────────────────────────────────────────────
    # 4a) 每個 user 在他現在的 org 都加一筆 OrgMembership(is_default=true)。
    #     使用 gen_random_uuid()(Postgres 13+ pgcrypto/uuid-ossp 內建)。
    op.execute(
        """
        INSERT INTO org_memberships (id, username, organization_id, role_id, is_default, status, joined_at)
        SELECT
            gen_random_uuid()::text,
            u.username,
            u.organization_id,
            u.role_id,
            true,
            'active',
            COALESCE(u.created_at, NOW())
        FROM users u
        WHERE u.organization_id IS NOT NULL
        ON CONFLICT (username, organization_id) DO NOTHING
        """
    )

    # 4b) 每個 user × project(同 org)都加一筆 ProjectMember,grandfather 既有 role_id。
    op.execute(
        """
        INSERT INTO project_members (id, project_id, username, role_id, status, joined_at)
        SELECT
            gen_random_uuid()::text,
            p.id,
            u.username,
            u.role_id,
            'active',
            NOW()
        FROM projects p
        JOIN users u ON u.organization_id = p.organization_id
        WHERE p.organization_id IS NOT NULL
        ON CONFLICT (project_id, username) DO NOTHING
        """
    )

    # 4c) roles.scope 已經由 server_default='org' 自動填值;不需要額外 UPDATE。


def downgrade() -> None:
    op.drop_column("roles", "scope")
    op.drop_index("ix_project_members_username", table_name="project_members")
    op.drop_index("ix_project_members_project", table_name="project_members")
    op.drop_table("project_members")
    op.drop_index("ix_org_memberships_org", table_name="org_memberships")
    op.drop_index("ix_org_memberships_username", table_name="org_memberships")
    op.drop_table("org_memberships")
