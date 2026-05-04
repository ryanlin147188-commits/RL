"""Authorization scope helpers for IDOR protection (Layer 1 stop-gap).

Background
----------
The AuthMiddleware in `app/middleware.py` already requires a valid JWT for any
`/api/*` request. That blocks anonymous attackers, but it does NOT prevent an
authenticated user in organization A from reading rows owned by organization B
(because most business endpoints fetch by primary key without an org filter).

This module provides reusable helpers so business routers can apply the same
"scope by user's organization" rule that `routers/projects.py` already uses,
without inventing the pattern eighteen separate times.

Pattern: derive `organization_id` via JOIN to Project
-----------------------------------------------------
Most business tables hold a `project_id` foreign key. Project itself owns the
canonical `organization_id`. So instead of denormalising `organization_id`
onto every business table (and writing eighteen migrations), we keep the
single source of truth on Project and JOIN through it on read paths.

Tables WITHOUT a direct `project_id` (e.g. `execution_step_log`) traverse one
extra hop through their parent (e.g. `execution_report.project_id`). The
helpers here support both shapes.

Phase 2.6 — per-project membership tightening
---------------------------------------------
Beyond the org check, every helper here now also requires an active
:class:`ProjectMember` row for the (user, project) pair. The
0006_multi_tenant_assignment migration grandfathered every existing user into
every project of their org, so this is zero regression for legacy callers;
once an admin removes a user via /api/projects/{pid}/members the
helpers immediately stop returning that project's rows from listing endpoints
(``scope_by_project``) and 404 on any direct entity-id lookup
(``ensure_project_in_scope``) or 403 on writes (``ensure_project_writable``).

Superusers bypass scope filtering entirely. The intent is single-tenant
self-hosted deployments where one designated admin needs unrestricted access
for support tasks.

Usage
-----
    from app.auth.scope import scope_by_project, ensure_project_in_scope

    @router.get("/defects")
    async def list_defects(
        user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db),
    ):
        stmt = select(Defect).order_by(Defect.created_at.desc())
        stmt = scope_by_project(stmt, Defect, user)
        return (await db.execute(stmt)).scalars().all()

    @router.get("/defects/{defect_id}")
    async def get_defect(
        defect_id: str,
        user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db),
    ):
        d = await db.get(Defect, defect_id)
        await ensure_project_in_scope(db, d.project_id if d else None, user)
        if not d:
            raise HTTPException(404, "Defect not found")
        return d
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import HTTPException
from sqlalchemy import Select, and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.project import Project
from app.models.project_member import ProjectMember
from app.models.user import User


def scope_by_project(
    stmt: Select,
    model: Any,
    user: User,
    *,
    project_id_attr: str = "project_id",
) -> Select:
    """Constrain `stmt` so it only returns rows of `model` whose project belongs
    to the user's organization AND in which the user is an active ProjectMember.

    `model.project_id_attr` is the attribute name holding the FK to projects.id.
    Default is `project_id`. Pass a different name when the FK column is named
    differently (rare).

    Phase 2.6: adds the ProjectMember join so listing endpoints automatically
    hide rows from projects the user has been removed from. Grandfather backfill
    means existing users see no behaviour change until an admin actively removes
    a member.
    """
    if user.is_superuser:
        return stmt
    project_fk = getattr(model, project_id_attr)
    return (
        stmt.join(Project, project_fk == Project.id)
        .join(
            ProjectMember,
            and_(
                ProjectMember.project_id == Project.id,
                ProjectMember.username == user.username,
                ProjectMember.status == "active",
            ),
        )
        .where(Project.organization_id == user.organization_id)
    )


async def _is_active_project_member(
    db: AsyncSession,
    project_id: str,
    username: str,
) -> bool:
    pm = (
        await db.execute(
            select(ProjectMember.id)
            .where(ProjectMember.project_id == project_id)
            .where(ProjectMember.username == username)
            .where(ProjectMember.status == "active")
        )
    ).scalar_one_or_none()
    return pm is not None


async def ensure_project_in_scope(
    db: AsyncSession,
    project_id: Optional[str],
    user: User,
    *,
    not_found_detail: str = "Resource not found",
) -> None:
    """Raise 404 unless ``project_id`` is in the user's org **and** the user has
    an active :class:`ProjectMember` row for it.

    Pass `None` to indicate the row itself was not found; this also raises 404,
    so the caller does not have to disambiguate "missing" vs "wrong org" before
    the response is sent (and we do not leak existence across orgs).

    Phase 2.6: also enforces project membership. 404 (not 403) for the
    non-member case so that a removed user can't probe project IDs and confirm
    they exist somewhere else in their org.
    """
    if user.is_superuser:
        if project_id is None:
            raise HTTPException(status_code=404, detail=not_found_detail)
        return
    if project_id is None:
        raise HTTPException(status_code=404, detail=not_found_detail)
    proj = await db.get(Project, project_id)
    if proj is None or proj.organization_id != user.organization_id:
        raise HTTPException(status_code=404, detail=not_found_detail)
    if not await _is_active_project_member(db, project_id, user.username):
        raise HTTPException(status_code=404, detail=not_found_detail)


async def ensure_project_writable(
    db: AsyncSession,
    project_id: str,
    user: User,
) -> None:
    """Same as `ensure_project_in_scope` but for create / update payloads where
    the caller is asserting that they may write to the supplied project.

    Raises:
        404: project does not exist at all.
        403: project exists but is in a different org, OR the user is in the
             org but not an active member of this specific project.

    Distinct status codes vs ``ensure_project_in_scope`` so API clients can
    distinguish "the resource you're writing to does not exist" from "you tried
    to write to a project you don't have membership in".
    """
    if user.is_superuser:
        return
    proj = await db.get(Project, project_id)
    if proj is None:
        raise HTTPException(status_code=404, detail="Project not found")
    if proj.organization_id != user.organization_id:
        raise HTTPException(
            status_code=403,
            detail="Cannot write to a project outside your organization",
        )
    if not await _is_active_project_member(db, project_id, user.username):
        raise HTTPException(
            status_code=403,
            detail="You are not a member of this project",
        )


async def ensure_object_in_scope_via_parent(
    db: AsyncSession,
    parent_model: Any,
    parent_id: Optional[str],
    user: User,
    *,
    parent_project_attr: str = "project_id",
    not_found_detail: str = "Resource not found",
) -> None:
    """For tables that do not hold `project_id` directly (e.g. `testcase_content`
    rows which live under a `tree_node`, or `execution_step_log` rows which live
    under an `execution_report`), look up the parent and check its project's
    organization.

    Raises 404 on missing parent or cross-org access.
    """
    if parent_id is None:
        raise HTTPException(status_code=404, detail=not_found_detail)
    parent = await db.get(parent_model, parent_id)
    if parent is None:
        raise HTTPException(status_code=404, detail=not_found_detail)
    project_id = getattr(parent, parent_project_attr, None)
    await ensure_project_in_scope(db, project_id, user, not_found_detail=not_found_detail)
