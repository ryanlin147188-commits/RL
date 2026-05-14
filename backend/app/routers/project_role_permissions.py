"""Per-project role permission override REST endpoints(v1.1.6)。

一個 ``(project, role)`` 一筆 override row。`require_casbin` 在這個專案內
看到該 role 時會用 override 的 permissions,而不是全域 Role.permissions_json。

* ``GET    /api/projects/{pid}/role-permissions``
    列出該專案內**所有 project-scope role** 的有效權限(有 override → override
    的;沒 override → fallback 到 role.permissions_json)。SPA「本專案角色權限」
    section 用此一次性把 4 row 拉齊。
* ``PUT    /api/projects/{pid}/role-permissions/{role_id}``
    upsert override。body `{permissions_json: [...]}`。
* ``DELETE /api/projects/{pid}/role-permissions/{role_id}``
    拿掉 override(該專案內該 role 回到全域預設)。
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.auth.permissions import require_casbin
from app.auth.permissions_catalog import P, ALL_PERMISSIONS
from app.database import get_db
from app.models.project import Project
from app.models.project_role_permission import ProjectRolePermission
from app.models.role import Role
from app.models.user import User
from app.routers.projects import _can_manage_project_members, _check_org_or_404

router = APIRouter()


# ── Pydantic ──────────────────────────────────────────────────────────


class RolePermissionUpdateRequest(BaseModel):
    permissions_json: list[str]


# ── 共用驗證 ──────────────────────────────────────────────────────────


def _validate_permissions(perms: list[str]) -> None:
    if not isinstance(perms, list):
        raise HTTPException(400, "permissions_json 必須是陣列")
    unknown = [p for p in perms if p not in ALL_PERMISSIONS]
    if unknown:
        raise HTTPException(400, f"未知的 permission key: {unknown}")


async def _require_manage_member(
    project_id: str, user: User, db: AsyncSession,
) -> Project:
    proj = await db.get(Project, project_id)
    _check_org_or_404(proj, user)
    if not _can_manage_project_members(user, proj):
        raise HTTPException(403, "需要組織管理員權限才能管理專案內角色權限")
    return proj


# ── GET — 列本專案內所有 project-scope role 的有效權限 ────────────────


@router.get(
    "/projects/{project_id}/role-permissions",
    tags=["G · 專案"],
    dependencies=[Depends(require_casbin(P.SETTINGS_READ))],
)
async def list_project_role_permissions(
    project_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """回該專案內所有 project-scope role 一覽,標明 override / fallback。

    response shape::

        [
          {
            "role_id": "<uuid>",
            "role_name": "Project-Tester",
            "default_permissions": [...],   # 全域 Role.permissions_json
            "effective_permissions": [...], # override or default
            "is_override": true|false,
            "override_id": "<uuid>"|null,
          },
          ...
        ]
    """
    proj = await db.get(Project, project_id)
    _check_org_or_404(proj, user)

    # 所有 project-scope role(系統 7 個內含 4 個,加自訂)
    roles = (
        await db.execute(
            select(Role)
            .where(Role.scope == "project")
            .order_by(Role.name)
        )
    ).scalars().all()

    overrides = (
        await db.execute(
            select(ProjectRolePermission).where(
                ProjectRolePermission.project_id == project_id
            )
        )
    ).scalars().all()
    overrides_by_role: dict[str, ProjectRolePermission] = {
        ov.role_id: ov for ov in overrides
    }

    out: list[dict] = []
    for r in roles:
        ov = overrides_by_role.get(r.id)
        default_perms = list(r.permissions_json or [])
        out.append({
            "role_id": r.id,
            "role_name": r.name,
            "description": r.description,
            "default_permissions": default_perms,
            "effective_permissions": list(ov.permissions_json) if ov else default_perms,
            "is_override": ov is not None,
            "override_id": ov.id if ov else None,
        })
    return out


# ── PUT — upsert override ──────────────────────────────────────────────


@router.put(
    "/projects/{project_id}/role-permissions/{role_id}",
    tags=["G · 專案"],
    dependencies=[Depends(require_casbin(P.ROLE_MANAGE))],
)
async def upsert_project_role_permission(
    project_id: str,
    role_id: str,
    payload: RolePermissionUpdateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    proj = await _require_manage_member(project_id, user, db)
    role = await db.get(Role, role_id)
    if not role:
        raise HTTPException(404, "找不到該 role")
    if role.scope != "project":
        raise HTTPException(
            400, "只能對 scope=project 的 role 設 per-project override(org-scope 角色作用全 org,無 per-project 概念)",
        )
    _validate_permissions(payload.permissions_json)

    existing = (
        await db.execute(
            select(ProjectRolePermission)
            .where(ProjectRolePermission.project_id == project_id)
            .where(ProjectRolePermission.role_id == role_id)
        )
    ).scalar_one_or_none()

    if existing:
        existing.permissions_json = list(payload.permissions_json)
        await db.flush()
        result = existing
    else:
        new = ProjectRolePermission(
            project_id=project_id,
            role_id=role_id,
            permissions_json=list(payload.permissions_json),
        )
        db.add(new)
        await db.flush()
        result = new

    # 通知 Casbin 重灌 — alias role 寫 / 改 / 拿掉都靠這支
    from app.auth.casbin_sync import schedule_full_resync
    schedule_full_resync()

    return {
        "id": result.id,
        "project_id": project_id,
        "role_id": role_id,
        "role_name": role.name,
        "permissions_json": list(result.permissions_json),
        "updated_at": result.updated_at.isoformat() if result.updated_at else None,
    }


# ── DELETE — 拿掉 override 回到 fallback ───────────────────────────────


@router.delete(
    "/projects/{project_id}/role-permissions/{role_id}",
    status_code=204,
    tags=["G · 專案"],
    dependencies=[Depends(require_casbin(P.ROLE_MANAGE))],
)
async def delete_project_role_permission(
    project_id: str,
    role_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _require_manage_member(project_id, user, db)
    existing = (
        await db.execute(
            select(ProjectRolePermission)
            .where(ProjectRolePermission.project_id == project_id)
            .where(ProjectRolePermission.role_id == role_id)
        )
    ).scalar_one_or_none()
    if not existing:
        # 沒 override 就不算錯,直接 204 — 跟「重設為預設」語意一致
        return
    await db.delete(existing)
    await db.flush()

    from app.auth.casbin_sync import schedule_full_resync
    schedule_full_resync()
