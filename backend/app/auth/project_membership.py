"""Per-project membership / permission FastAPI dependencies.

Companion to :mod:`app.auth.permissions` — the existing ``require_permission``
checks org-level role only. With phase 2 of the multi-tenant assignment plan,
some routes need an additional check that the caller is a member of the
specific project they're targeting.

Two dependency factories live here:

* :func:`ensure_project_member` — minimal "are you a member" check, returns
  the :class:`ProjectMember` row (or ``None`` for superuser bypass) so the
  handler can read the per-project ``role_id`` if needed. Intended use:
  ``Depends(ensure_project_member)`` against any route that has ``project_id``
  as a path parameter and reads/writes resources scoped to that project.

* :func:`require_project_permission` — like :func:`require_permission` but
  resolves the effective role as ``ProjectMember.role_id`` if non-NULL,
  otherwise the user's :class:`OrgMembership` role for the active org.

Both dependencies grandfather superusers (always allowed).
"""
from __future__ import annotations

from typing import Callable, Optional

from fastapi import Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.database import get_db
from app.models.org_membership import OrgMembership
from app.models.project import Project
from app.models.project_member import ProjectMember
from app.models.role import Role
from app.models.user import User


async def _resolve_effective_role(
    db: AsyncSession,
    user: User,
    project_id: str,
) -> Optional[Role]:
    """Return the :class:`Role` that applies to ``user`` for ``project_id``.

    Resolution order:
        1. ``ProjectMember.role_id`` if non-NULL (per-project override).
        2. ``OrgMembership.role_id`` for the project's organization (org default).
        3. ``user.role_id`` (legacy User-level role; for backward compat with
           rows whose membership backfill hasn't run yet).
    """
    pm = (
        await db.execute(
            select(ProjectMember)
            .where(ProjectMember.project_id == project_id)
            .where(ProjectMember.username == user.username)
            .where(ProjectMember.status == "active")
        )
    ).scalar_one_or_none()
    if pm is not None and pm.role_id:
        return await db.get(Role, pm.role_id)

    proj = await db.get(Project, project_id)
    if proj is not None and proj.organization_id:
        om = (
            await db.execute(
                select(OrgMembership)
                .where(OrgMembership.username == user.username)
                .where(OrgMembership.organization_id == proj.organization_id)
                .where(OrgMembership.status == "active")
            )
        ).scalar_one_or_none()
        if om is not None and om.role_id:
            return await db.get(Role, om.role_id)

    if user.role_id:
        return await db.get(Role, user.role_id)
    return None


async def ensure_project_member(
    project_id: Optional[str] = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Optional[ProjectMember]:
    """Block requests where ``current_user`` is not a member of ``project_id``.

    ``project_id`` is sourced by FastAPI from the route's path or query
    parameters. Three cases:

    * ``project_id is None`` — caller didn't bind a project (e.g. an org-wide
      listing endpoint with ``project_id: Optional[str] = Query(None)``).
      Nothing to check; the route's own tenant-scoping handles isolation.
    * Superuser — bypass; return ``None``.
    * Otherwise — must have an active :class:`ProjectMember` row for that
      project, else 404 (not 403, to avoid leaking project existence).

    Returns the matching :class:`ProjectMember` row when found, or ``None``
    in the bypass / no-project cases. Use as
    ``dependencies=[Depends(ensure_project_member)]`` on any route with a
    ``project_id`` parameter.
    """
    if not project_id:
        return None
    if user.is_superuser:
        return None
    proj = await db.get(Project, project_id)
    if proj is None:
        raise HTTPException(404, "Project not found")
    pm = (
        await db.execute(
            select(ProjectMember)
            .where(ProjectMember.project_id == project_id)
            .where(ProjectMember.username == user.username)
            .where(ProjectMember.status == "active")
        )
    ).scalar_one_or_none()
    if pm is None:
        # 404 not 403: don't leak project existence to non-members.
        raise HTTPException(404, "Project not found")
    return pm


def require_project_permission(*needed: str) -> Callable:
    """Build a FastAPI dependency that asserts the caller has every permission
    in ``needed`` for the project identified by the path/query parameter
    ``project_id``.

    Permission resolution: see :func:`_resolve_effective_role`.

    Raises:
        404: not a member (same reason as :func:`ensure_project_member`).
        403: member, but the resolved role is missing one or more permissions.
    """
    if not needed:
        raise ValueError("require_project_permission() requires at least one permission key")

    async def _check(
        project_id: str,
        user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db),
    ) -> User:
        if user.is_superuser:
            return user
        # Membership first (404 if missing) — same as ensure_project_member.
        proj = await db.get(Project, project_id)
        if proj is None:
            raise HTTPException(404, "Project not found")
        pm = (
            await db.execute(
                select(ProjectMember)
                .where(ProjectMember.project_id == project_id)
                .where(ProjectMember.username == user.username)
                .where(ProjectMember.status == "active")
            )
        ).scalar_one_or_none()
        if pm is None:
            raise HTTPException(404, "Project not found")

        effective_role = await _resolve_effective_role(db, user, project_id)
        granted: set[str] = set()
        if effective_role is not None and effective_role.permissions_json:
            granted = set(effective_role.permissions_json)

        missing = [p for p in needed if p not in granted]
        if missing:
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "permission_denied",
                    "missing_permissions": missing,
                    "scope": "project",
                    "project_id": project_id,
                },
            )
        return user

    return _check
