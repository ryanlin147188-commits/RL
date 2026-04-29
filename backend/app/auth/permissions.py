"""RBAC ``require_permission`` FastAPI dependency.

Checks the calling user's :class:`Role` for every permission key listed at
the call site, returning 403 if any are missing. Superusers bypass.

Usage::

    from fastapi import Depends
    from app.auth.permissions import require_permission
    from app.auth.permissions_catalog import P

    @router.post(
        "/defects",
        dependencies=[Depends(require_permission(P.DEFECT_WRITE))],
    )
    async def create_defect(...):
        ...

    @router.delete(
        "/projects/{pid}",
        dependencies=[Depends(require_permission(P.PROJECT_WRITE, P.PROJECT_DELETE))],
    )
    async def delete_project(...):
        ...

The dependency does not return the user (uses ``dependencies=[]``); use
``get_current_user`` separately when the handler needs the user object.
"""
from __future__ import annotations

from typing import Callable

from fastapi import Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.database import get_db
from app.models.role import Role
from app.models.user import User


def require_permission(*needed: str) -> Callable:
    """Build a FastAPI dependency that asserts the current user holds every
    permission in ``needed``.

    Raises:
        HTTPException 403: missing one or more permissions; the response body
            ``detail`` lists the missing keys so the frontend can render a
            specific "you need X to do this" message rather than a vague 403.
    """
    if not needed:
        raise ValueError("require_permission() requires at least one permission key")

    async def _check(
        user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db),
    ) -> User:
        if user.is_superuser:
            return user

        granted: set[str] = set()
        if user.role_id is not None:
            role = await db.get(Role, user.role_id)
            if role is not None and role.permissions_json:
                granted = set(role.permissions_json)

        missing = [p for p in needed if p not in granted]
        if missing:
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "permission_denied",
                    "missing_permissions": missing,
                },
            )
        return user

    return _check
