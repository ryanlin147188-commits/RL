from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel
from sqlalchemy import asc, desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.auth.permissions import require_casbin
from app.auth.permissions_catalog import P
from app.auth.project_membership import ensure_project_member
from app.database import get_db
from app.models.group import Group, GroupMembership
from app.models.org_membership import OrgMembership
from app.models.project import Project
from app.models.project_member import ProjectMember
from app.models.role import Role
from app.models.tree_node import TreeNode
from app.models.user import User
from app.schemas.project import ProjectCreate, ProjectResponse, ProjectUpdate
from app.services.tree_service import build_tree

router = APIRouter()


# 統一 7 值狀態 — 舊 project lifecycle (Planning / Active / OnHold / Archived)
# 對應到統一狀態。外部呼叫送舊值會自動 normalize,送新值原樣保留。
_LEGACY_PROJECT_STATUS = {
    "Planning": "New",
    "Active": "InProgress",
    "OnHold": "Assigned",
    "Archived": "Closed",
}


def _normalize_project_status(val):
    if val is None:
        return None
    return _LEGACY_PROJECT_STATUS.get(val, val)


# 1. GET /api/projects
# 多租戶 phase 2:加 ProjectMember 過濾。grandfather migration 已把所有同 org 的
# user × project 寫進 ProjectMember,所以行為對既有使用者完全不變;管理員開始
# 從某 project 移除成員後,該 user 立刻看不到該 project。
#
# v1.1.10:拿掉 organization_id 過濾,純粹靠 ProjectMember JOIN 控制可見性。
# 這樣被邀請進別人 org 專案的協作者(ProjectInvite redeem 後)就能在 sidebar
# 看到該專案,即使他的 personal org 跟 project.organization_id 不同。
@router.get(
    "/projects",
    response_model=list[ProjectResponse],
    dependencies=[Depends(require_casbin(P.PROJECT_READ))],
)
async def list_projects(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(Project).order_by(Project.created_at.desc())
    if not user.is_superuser:
        # 只回 current_user 是 active member 的 projects(跨 org 也算)。
        stmt = stmt.join(
            ProjectMember,
            (ProjectMember.project_id == Project.id)
            & (ProjectMember.username == user.username)
            & (ProjectMember.status == "active"),
        )
    result = await db.execute(stmt)
    return result.scalars().all()


# 2. POST /api/projects
@router.post(
    "/projects",
    response_model=ProjectResponse,
    status_code=201,
    dependencies=[Depends(require_casbin(P.PROJECT_WRITE))],
)
async def create_project(
    payload: ProjectCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    project = Project(
        name=payload.name,
        organization_id=user.organization_id,   # 自動掛在使用者的 org
        description=payload.description,
        owner=payload.owner,
        # 統一 7 值狀態 — Planning→New, Active→InProgress, OnHold→Assigned, Archived→Closed
        status=_normalize_project_status(payload.status) or "InProgress",
        start_date=payload.start_date,
        target_date=payload.target_date,
        tags=payload.tags,
    )
    db.add(project)
    await db.flush()
    # 建立者自動成為這個 project 的 member;role_id=NULL = 從 OrgMembership 繼承,
    # 這樣建立者(通常是 admin)馬上就有完整權限,不用再走一次 add-member 流程。
    db.add(ProjectMember(
        project_id=project.id,
        username=user.username,
        role_id=None,
        status="active",
    ))
    await db.flush()
    # 仁慈模式:同 org 所有 active user 都自動加入此 project,讓他們的
    # list_projects 看得到。建立者已加過,helper 內部會用 NOT IN 跳過。
    try:
        from app.auth.project_membership import ensure_project_has_all_org_users
        await ensure_project_has_all_org_users(db, project)
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning("ensure_project_has_all_org_users failed: %s", e)
    await db.refresh(project)
    return project


async def _check_org_or_404(
    proj: Optional[Project], user: User, db: AsyncSession,
) -> Project:
    """共用:找不到、或不是該 project 的成員都回 404(不洩漏「跨 org 存在」資訊)。

    v1.1.10:cross-org 放行 — 同 org 直接通過;跨 org 但有 active ProjectMember row
    (走 ProjectInvite redeem 進來的協作者)也通過。其餘 404。
    """
    if proj is None:
        raise HTTPException(status_code=404, detail="Project not found")
    if user.is_superuser:
        return proj
    if proj.organization_id == user.organization_id:
        return proj
    # 跨 org:檢查是否有 active ProjectMember row
    pm = (
        await db.execute(
            select(ProjectMember)
            .where(ProjectMember.project_id == proj.id)
            .where(ProjectMember.username == user.username)
            .where(ProjectMember.status == "active")
        )
    ).scalar_one_or_none()
    if pm is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return proj


# 3. GET /api/projects/{projectId}/tree
@router.get(
    "/projects/{project_id}/tree",
    dependencies=[
        Depends(require_casbin(P.PROJECT_READ)),
        Depends(ensure_project_member),
    ],
)
async def get_project_tree(
    project_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """一次撈出整棵樹，回傳巢狀 JSON（核心 API）。"""
    proj = await db.get(Project, project_id)
    await _check_org_or_404(proj, user, db)

    result = await db.execute(
        select(TreeNode)
        .where(TreeNode.project_id == project_id)
        .order_by(TreeNode.sort_order)
    )
    nodes = result.scalars().all()
    return build_tree(nodes, parent_id=None)


# 4. DELETE /api/projects/{projectId}
@router.delete(
    "/projects/{project_id}",
    status_code=204,
    dependencies=[
        Depends(require_casbin(P.PROJECT_DELETE)),
        Depends(ensure_project_member),
    ],
)
async def delete_project(
    project_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """刪除專案（連同樹狀節點、測試案例、執行報告等一併由 DB cascade 刪除）。"""
    proj = await db.get(Project, project_id)
    proj = await _check_org_or_404(proj, user, db)
    await db.delete(proj)
    await db.commit()
    return None


# 5. PUT /api/projects/{projectId}
@router.put(
    "/projects/{project_id}",
    response_model=ProjectResponse,
    dependencies=[
        Depends(require_casbin(P.PROJECT_WRITE)),
        Depends(ensure_project_member),
    ],
)
async def update_project(
    project_id: str,
    payload: ProjectUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """更新測試專案的欄位（部分更新，未提供的欄位保留）。"""
    proj = await db.get(Project, project_id)
    proj = await _check_org_or_404(proj, user, db)
    data = payload.model_dump(exclude_unset=True)
    if "name" in data:
        name = (data["name"] or "").strip()
        if not name:
            raise HTTPException(status_code=400, detail="Project name cannot be empty")
        proj.name = name
    for key in ("description", "owner", "status", "start_date", "target_date", "tags"):
        if key in data:
            val = data[key]
            if key == "status":
                val = _normalize_project_status(val)
            setattr(proj, key, val)
    await db.flush()
    await db.refresh(proj)
    return proj


# 6. GET /api/projects/{projectId} — 單一測試專案詳情
@router.get(
    "/projects/{project_id}",
    response_model=ProjectResponse,
    dependencies=[
        Depends(require_casbin(P.PROJECT_READ)),
        Depends(ensure_project_member),
    ],
)
async def get_project(
    project_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    proj = await db.get(Project, project_id)
    return await _check_org_or_404(proj, user, db)


# ─────────────────── Project Members CRUD(phase 2)───────────────────
# 一個 project 內誰是成員 + 該成員在這 project 的角色(可 override OrgMembership 的角色)。
# 權限檢查走 _check_org_or_404 + 呼叫者必須是 superuser 或 ProjectMember,
# 進一步「能否管理成員」交給 require_casbin(USER_MANAGE) 守。

def _can_manage_project_members(user: User, proj: Project) -> bool:
    """superuser 或同 org 的 admin(P.USER_MANAGE)能管理。前端會用 me/orgs 判斷。"""
    return bool(user.is_superuser) or user.organization_id == proj.organization_id


@router.get("/projects/{project_id}/assignable-users", tags=["G · 專案"])
async def list_project_assignable_users(
    project_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """列出該專案的成員(active + user.is_active),前端指派 picker 用。
    取代 ``/api/auth/users/assignable`` 在 project-scoped 場景的角色 — 那一支
    只看 organization_id,會把專案外的使用者也列出來。
    """
    proj = await db.get(Project, project_id)
    await _check_org_or_404(proj, user, db)
    rows = (
        await db.execute(
            select(User)
            .join(ProjectMember, ProjectMember.username == User.username)
            .where(ProjectMember.project_id == project_id)
            .where(ProjectMember.status == "active")
            .where(User.is_active.is_(True))
            .order_by(User.username)
        )
    ).scalars().all()
    return [
        {
            "username": u.username,
            "display_name": u.display_name,
            "email": u.email,
            "avatar_url": u.avatar_url,
        }
        for u in rows
    ]


_PROJ_MEMBER_SORT_COLS = {
    "username": User.username,
    "email": User.email,
    "display_name": User.display_name,
    "role_name": Role.name,
    "status": ProjectMember.status,
    "joined_at": ProjectMember.joined_at,
}


@router.get("/projects/{project_id}/members", tags=["G · 專案"])
async def list_project_members(
    project_id: str,
    response: Response,
    search: Optional[str] = None,
    sort_by: Optional[str] = None,
    sort_dir: str = "asc",
    limit: Optional[int] = None,
    offset: int = 0,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """列出某專案的所有成員。可選 `?search=&sort_by=&sort_dir=&limit=&offset=`。
    帶 `limit` 時會在 response header 加 `X-Total-Count`。"""
    proj = await db.get(Project, project_id)
    await _check_org_or_404(proj, user, db)

    base_join = (
        select(ProjectMember, User, Role)
        .join(User, User.username == ProjectMember.username)
        .outerjoin(Role, Role.id == ProjectMember.role_id)
        .where(ProjectMember.project_id == project_id)
    )
    count_join = (
        select(func.count())
        .select_from(ProjectMember)
        .join(User, User.username == ProjectMember.username)
        .outerjoin(Role, Role.id == ProjectMember.role_id)
        .where(ProjectMember.project_id == project_id)
    )

    if search:
        q = f"%{search.strip().lower()}%"
        cond = or_(
            func.lower(User.username).like(q),
            func.lower(func.coalesce(User.email, "")).like(q),
            func.lower(func.coalesce(User.display_name, "")).like(q),
        )
        base_join = base_join.where(cond)
        count_join = count_join.where(cond)

    sort_col = _PROJ_MEMBER_SORT_COLS.get((sort_by or "").strip()) or User.username
    direction = desc if (sort_dir or "asc").lower() == "desc" else asc
    base_join = base_join.order_by(direction(sort_col))

    if limit is not None:
        total = (await db.execute(count_join)).scalar_one() or 0
        response.headers["X-Total-Count"] = str(total)
        try:
            limit_int = max(1, min(int(limit), 500))
        except (TypeError, ValueError):
            limit_int = 50
        try:
            offset_int = max(0, int(offset))
        except (TypeError, ValueError):
            offset_int = 0
        base_join = base_join.limit(limit_int).offset(offset_int)

    rows = (await db.execute(base_join)).all()
    return [
        {
            "id": pm.id,
            "username": u.username,
            "display_name": u.display_name,
            "email": u.email,
            # ``role_id`` / ``role_name`` 是 ProjectMember.role_id(本專案 override);
            # NULL = 沿用全域 role。``global_role_id`` 是 ``users.role_id``(全域),
            # 給「編輯使用者」modal 預填用,避免 modal 改全域 role 後重整顯示
            # 「無角色」的錯位(此 modal PUT /auth/users/{u} 改的就是全域)。
            "role_id": role.id if role else None,
            "role_name": role.name if role else None,
            "role_scope": role.scope if role else None,
            "global_role_id": u.role_id,
            "status": pm.status,
            "joined_at": pm.joined_at.isoformat() if pm.joined_at else None,
            # 給「編輯使用者」modal 預填用(superuser 才看得到該按鈕在後端 PUT/DELETE 上的效力)
            "is_active": bool(u.is_active),
            "is_superuser": bool(u.is_superuser),
        }
        for pm, u, role in rows
    ]


@router.post("/projects/{project_id}/members", status_code=201, tags=["G · 專案"])
async def add_project_member(
    project_id: str,
    payload: dict,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """加成員到專案。body `{"username": "...", "role_id": "..." | null}`。"""
    proj = await db.get(Project, project_id)
    await _check_org_or_404(proj, user, db)
    if not _can_manage_project_members(user, proj):
        raise HTTPException(403, "需要組織管理員權限才能管理專案成員")
    target_username = (payload or {}).get("username", "").strip()
    role_id = (payload or {}).get("role_id") or None
    if not target_username:
        raise HTTPException(400, "缺少 username")
    target = await db.get(User, target_username)
    if not target:
        raise HTTPException(404, "找不到該使用者")
    # 必要前提:該 user 必須先是這個 org 的 OrgMembership(避免跨 org 加成員)。
    from app.models.org_membership import OrgMembership
    om = (
        await db.execute(
            select(OrgMembership)
            .where(OrgMembership.username == target_username)
            .where(OrgMembership.organization_id == proj.organization_id)
            .where(OrgMembership.status == "active")
        )
    ).scalar_one_or_none()
    if not om and not target.is_superuser:
        raise HTTPException(400, "該使用者不是此專案組織的成員,請先把他加進組織")
    if role_id:
        role = await db.get(Role, role_id)
        if not role:
            raise HTTPException(400, "無效的 role_id")
    existing = (
        await db.execute(
            select(ProjectMember)
            .where(ProjectMember.project_id == project_id)
            .where(ProjectMember.username == target_username)
        )
    ).scalar_one_or_none()
    if existing:
        raise HTTPException(409, "該使用者已是此專案成員")
    pm = ProjectMember(
        project_id=project_id,
        username=target_username,
        role_id=role_id,
        status="active",
        invited_by=user.username,
    )
    db.add(pm)
    await db.flush()
    from app.auth.casbin_sync import schedule_user_resync
    schedule_user_resync(target_username)
    return {"id": pm.id, "project_id": project_id, "username": target_username}


# ─── Tier B5:bulk 改 per-project role / status ──────────────────────
@router.patch("/projects/{project_id}/members/bulk", tags=["G · 專案"])
async def bulk_update_project_members(
    project_id: str,
    payload: dict,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """一次更新多筆 ProjectMember 的 role_id / status。
    body: `{"usernames": [...], "role_id": "..." | null, "status": "active"}`
    role_id=null 表示繼承 OrgMembership 角色。"""
    proj = await db.get(Project, project_id)
    await _check_org_or_404(proj, user, db)
    if not _can_manage_project_members(user, proj):
        raise HTTPException(403, "需要組織管理員權限才能管理專案成員")

    body = payload or {}
    usernames = body.get("usernames") or []
    if not isinstance(usernames, list) or not usernames:
        raise HTTPException(400, "缺少 usernames(非空陣列)")
    if len(usernames) > 200:
        raise HTTPException(400, "單次最多 200 筆")

    has_role = "role_id" in body
    role_id = body.get("role_id") or None if has_role else None
    if has_role and role_id:
        role = await db.get(Role, role_id)
        if not role:
            raise HTTPException(400, "無效的 role_id")

    has_status = "status" in body
    new_status = (body.get("status") or "").strip() if has_status else None
    if has_status and new_status not in ("active", "invited", "disabled"):
        raise HTTPException(400, "status 必須是 active / invited / disabled")

    if not has_role and not has_status:
        raise HTTPException(400, "至少要指定 role_id 或 status 其中一個")

    updated = 0
    skipped: list[dict] = []
    touched: list[str] = []
    for u in usernames:
        u = (u or "").strip()
        if not u:
            continue
        pm = (await db.execute(
            select(ProjectMember)
            .where(ProjectMember.project_id == project_id)
            .where(ProjectMember.username == u)
        )).scalar_one_or_none()
        if not pm:
            skipped.append({"username": u, "reason": "not a project member"})
            continue
        if has_role:
            pm.role_id = role_id
        if has_status:
            pm.status = new_status
        updated += 1
        touched.append(u)
    await db.flush()
    from app.auth.casbin_sync import schedule_user_resync
    for u in touched:
        schedule_user_resync(u)
    return {"updated": updated, "skipped": skipped}


# ─── C5:從群組批次加入專案成員 ──────────────────────────────────
@router.post("/projects/{project_id}/members/from-group", tags=["G · 專案"])
async def add_project_members_from_group(
    project_id: str,
    payload: dict,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """C5 — 把整個群組(含子群組)展開後加為專案成員。
    body:`{"group_id": "...", "role_id": null, "include_descendants": true}`
    回傳:`{added: N, skipped: [{username, reason}]}`。
    跳過原因:已在此專案 / 不在此 org 的 OrgMembership / user 不存在。"""
    proj = await db.get(Project, project_id)
    await _check_org_or_404(proj, user, db)
    if not _can_manage_project_members(user, proj):
        raise HTTPException(403, "需要組織管理員權限才能管理專案成員")
    group_id = (payload or {}).get("group_id")
    role_id = (payload or {}).get("role_id") or None
    include_desc = bool((payload or {}).get("include_descendants", True))
    if not group_id:
        raise HTTPException(400, "缺少 group_id")
    root = await db.get(Group, group_id)
    if not root:
        raise HTTPException(404, "找不到該群組")
    if root.organization_id and root.organization_id != proj.organization_id:
        raise HTTPException(400, "群組與專案不在同一個 organization")
    if role_id:
        role = await db.get(Role, role_id)
        if not role:
            raise HTTPException(400, "無效的 role_id")

    # 1) BFS 展開群組樹
    group_ids: set[str] = {group_id}
    if include_desc:
        frontier = {group_id}
        while frontier:
            children = (await db.execute(
                select(Group.id).where(Group.parent_id.in_(frontier))
            )).scalars().all()
            new_ids = set(children) - group_ids
            if not new_ids:
                break
            group_ids |= new_ids
            frontier = new_ids

    # 2) 抓所有(unique)group member usernames
    usernames = set((await db.execute(
        select(GroupMembership.username).where(GroupMembership.group_id.in_(group_ids))
    )).scalars().all())
    if not usernames:
        return {"added": 0, "skipped": [], "expanded_groups": len(group_ids)}

    # 3) 一次撈現有 ProjectMember + OrgMembership(降 N+1)
    existing_pm = set((await db.execute(
        select(ProjectMember.username)
        .where(ProjectMember.project_id == project_id)
        .where(ProjectMember.username.in_(usernames))
    )).scalars().all())
    in_org = set((await db.execute(
        select(OrgMembership.username)
        .where(OrgMembership.organization_id == proj.organization_id)
        .where(OrgMembership.username.in_(usernames))
        .where(OrgMembership.status == "active")
    )).scalars().all())

    added = 0
    skipped: list[dict] = []
    new_usernames: list[str] = []
    for u in sorted(usernames):
        if u in existing_pm:
            skipped.append({"username": u, "reason": "已是專案成員"}); continue
        if u not in in_org:
            skipped.append({"username": u, "reason": "不是此 org 的 active 成員"}); continue
        db.add(ProjectMember(
            project_id=project_id,
            username=u,
            role_id=role_id,
            status="active",
            invited_by=user.username,
        ))
        added += 1
        new_usernames.append(u)
    await db.flush()
    from app.auth.casbin_sync import schedule_user_resync
    for u in new_usernames:
        schedule_user_resync(u)
    return {
        "added": added,
        "skipped": skipped,
        "expanded_groups": len(group_ids),
    }


@router.patch("/projects/{project_id}/members/{username}", tags=["G · 專案"])
async def update_project_member(
    project_id: str,
    username: str,
    payload: dict,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """改成員的角色或狀態。body 可含 `role_id`(NULL 代表繼承 org-level)/ `status`。"""
    proj = await db.get(Project, project_id)
    await _check_org_or_404(proj, user, db)
    if not _can_manage_project_members(user, proj):
        raise HTTPException(403, "需要組織管理員權限才能管理專案成員")
    pm = (
        await db.execute(
            select(ProjectMember)
            .where(ProjectMember.project_id == project_id)
            .where(ProjectMember.username == username)
        )
    ).scalar_one_or_none()
    if not pm:
        raise HTTPException(404, "找不到此成員")
    if "role_id" in (payload or {}):
        role_id = payload["role_id"] or None
        if role_id:
            role = await db.get(Role, role_id)
            if not role:
                raise HTTPException(400, "無效的 role_id")
        pm.role_id = role_id
    if "status" in (payload or {}):
        new_status = (payload["status"] or "").strip()
        if new_status not in ("active", "invited", "disabled"):
            raise HTTPException(400, "status 必須是 active / invited / disabled")
        pm.status = new_status
    await db.flush()
    from app.auth.casbin_sync import schedule_user_resync
    schedule_user_resync(username)
    return {"ok": True, "id": pm.id, "role_id": pm.role_id, "status": pm.status}


@router.delete("/projects/{project_id}/members/{username}", status_code=204, tags=["G · 專案"])
async def remove_project_member(
    project_id: str,
    username: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """從專案移除成員(該 user 仍保留 OrgMembership,只是看不到此專案了)。"""
    proj = await db.get(Project, project_id)
    await _check_org_or_404(proj, user, db)
    if not _can_manage_project_members(user, proj):
        raise HTTPException(403, "需要組織管理員權限才能管理專案成員")
    pm = (
        await db.execute(
            select(ProjectMember)
            .where(ProjectMember.project_id == project_id)
            .where(ProjectMember.username == username)
        )
    ).scalar_one_or_none()
    if not pm:
        raise HTTPException(404, "找不到此成員")
    if username == user.username and not user.is_superuser:
        raise HTTPException(400, "不可移除自己;要自己退出專案請走 DELETE /projects/{id}/leave")
    await db.delete(pm)
    await db.flush()
    from app.auth.casbin_sync import schedule_user_resync
    schedule_user_resync(username)


@router.delete(
    "/projects/{project_id}/leave",
    status_code=204,
    tags=["G · 專案"],
)
async def leave_project(
    project_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """使用者自助退出專案(v1.1.10)。

    不需 USER_MANAGE 權限 — 只要是該專案的 active ProjectMember 就能退出
    (對應「設定 → 退出專案」UI)。退出後 sidebar 看不到該 project,
    要再回去得請 admin 重新邀請。

    擋條件:
    * 找不到 active ProjectMember → 404
    * 退完後該 project 沒有任何 active member 了(避免孤兒化)→ 400
    """
    proj = await db.get(Project, project_id)
    if proj is None:
        raise HTTPException(404, "找不到專案")
    pm = (
        await db.execute(
            select(ProjectMember)
            .where(ProjectMember.project_id == project_id)
            .where(ProjectMember.username == user.username)
            .where(ProjectMember.status == "active")
        )
    ).scalar_one_or_none()
    if pm is None:
        raise HTTPException(404, "您不是此專案的成員")
    # 退完後是否就沒任何 active member 了?是 → 擋,避免孤兒。
    remaining = (
        await db.execute(
            select(ProjectMember)
            .where(ProjectMember.project_id == project_id)
            .where(ProjectMember.username != user.username)
            .where(ProjectMember.status == "active")
            .limit(1)
        )
    ).scalar_one_or_none()
    if remaining is None:
        raise HTTPException(
            400,
            "您是此專案唯一的成員,退出後將沒人能管理。請改用「刪除專案」,或邀請其他人接手後再退出",
        )
    await db.delete(pm)
    await db.flush()
    from app.auth.casbin_sync import schedule_user_resync
    schedule_user_resync(user.username)


# ─────────────────── Clone Project ───────────────────────────────────

class _CloneProjectPayload(BaseModel):
    name: str


@router.post(
    "/projects/{project_id}/clone",
    response_model=ProjectResponse,
    status_code=201,
    dependencies=[
        Depends(require_casbin(P.PROJECT_WRITE)),
        Depends(ensure_project_member),
    ],
)
async def clone_project(
    project_id: str,
    payload: _CloneProjectPayload,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """複製一個現有專案的完整樹狀結構（含 TestcaseContent 與前置案例連結）到新專案。"""
    import copy
    import uuid as _uuid
    from app.models.testcase_content import TestcaseContent
    from app.models.testcase_precondition_link import TestcasePreconditionLink

    src = await db.get(Project, project_id)
    await _check_org_or_404(src, user, db)

    # 1) 建立新專案
    new_proj = Project(
        name=payload.name.strip(),
        organization_id=user.organization_id,
        status="InProgress",
    )
    db.add(new_proj)
    await db.flush()

    # 建立者自動成為成員
    db.add(ProjectMember(project_id=new_proj.id, username=user.username, role_id=None, status="active"))
    await db.flush()

    # 2) 取出原專案所有節點（含 testcase_content），按 sort_order 排序確保父節點先處理
    result = await db.execute(
        select(TreeNode)
        .where(TreeNode.project_id == project_id)
        .order_by(TreeNode.sort_order)
    )
    src_nodes = result.scalars().all()

    # 3) 取出所有 TestcaseContent（以 node_id 為 key）
    if src_nodes:
        tc_ids = [n.id for n in src_nodes if n.level_type.value == "TESTCASE"]
        tc_map: dict[str, TestcaseContent] = {}
        if tc_ids:
            tc_result = await db.execute(
                select(TestcaseContent).where(TestcaseContent.node_id.in_(tc_ids))
            )
            for tc in tc_result.scalars().all():
                tc_map[tc.node_id] = tc

    # 4) old_id → new_id 對照表
    id_map: dict[str, str] = {}
    for n in src_nodes:
        id_map[n.id] = str(_uuid.uuid4())

    # 5) 依序建立新節點 + content
    for n in src_nodes:
        new_node = TreeNode(
            id=id_map[n.id],
            project_id=new_proj.id,
            parent_id=id_map.get(n.parent_id) if n.parent_id else None,
            level_type=n.level_type,
            name=n.name,
            sort_order=n.sort_order,
        )
        db.add(new_node)

        if n.level_type.value == "TESTCASE" and n.id in tc_map:
            orig = tc_map[n.id]
            db.add(TestcaseContent(
                node_id=id_map[n.id],
                organization_id=orig.organization_id,
                ac_text=orig.ac_text,
                setup_text=orig.setup_text,
                steps_json=copy.deepcopy(orig.steps_json),
                ddt_json=copy.deepcopy(orig.ddt_json),
            ))

    await db.flush()

    # 6) 複製前置案例連結（兩端 node_id 都在本專案內才複製）
    if id_map:
        pre_result = await db.execute(
            select(TestcasePreconditionLink).where(
                TestcasePreconditionLink.testcase_id.in_(id_map.keys())
            )
        )
        for link in pre_result.scalars().all():
            # 前置案例也必須屬於同一專案；跨專案的前置不複製
            if link.precondition_testcase_id not in id_map:
                continue
            db.add(TestcasePreconditionLink(
                testcase_id=id_map[link.testcase_id],
                precondition_testcase_id=id_map[link.precondition_testcase_id],
                sort_order=link.sort_order,
                enabled=link.enabled,
                on_failure=link.on_failure,
                organization_id=link.organization_id,
                created_by=user.username,
            ))
        await db.flush()

    await db.refresh(new_proj)
    return new_proj


# ─────────────────────────────────────────────────────────────────
# Project Invites — pull-based onboarding
# ─────────────────────────────────────────────────────────────────
import logging
from datetime import datetime
from app.models.project_invite import ProjectInvite
from app.schemas.project_invite import (
    ProjectInviteCreate, ProjectInviteRedeem, ProjectInviteResponse,
)

_invite_logger = logging.getLogger(__name__)


def _send_invite_email(
    db_sync, *, to: str, project_name: str, invite_code: str,
    expires_at: datetime, inviter: str, redeem_url: str,
    organization_id: Optional[str],
) -> None:
    """Try to send the invite email. Failure is non-fatal — invite row stays,
    admin can copy-paste the code manually as fallback (frontend 會顯示 code)。"""
    from app.services.email_service import send_email_sync
    subject = f"[AutoTest] 邀請加入專案「{project_name}」"
    html = f"""\
<html><body style="font-family:system-ui,-apple-system,sans-serif;color:#1f2937;max-width:600px">
<h2 style="color:#f59e0b">AutoTest 專案邀請</h2>
<p>您好,</p>
<p><b>{inviter}</b> 邀請您加入 AutoTest 的專案「<b>{project_name}</b>」。</p>
<p>請先<b>登入 AutoTest</b> (建議用 Zoho SSO),登入後到「設定 → 兌換邀請碼」貼上下列邀請碼:</p>
<pre style="background:#f3f4f6;padding:12px;border-radius:6px;font-size:14px;font-weight:bold">{invite_code}</pre>
<p>或直接點此一鍵兌換:</p>
<p><a href="{redeem_url}" style="display:inline-block;padding:10px 18px;background:#f59e0b;color:white;text-decoration:none;border-radius:6px">登入並加入專案</a></p>
<p style="color:#6b7280;font-size:12px">邀請碼將於 {expires_at:%Y-%m-%d %H:%M} (UTC) 過期,僅限使用一次。</p>
<p style="color:#6b7280;font-size:12px">兌換時系統會驗證您登入的 email 必須是這封信寄到的地址,請用同一個 email 登入。</p>
<hr style="border:none;border-top:1px solid #e5e7eb;margin-top:24px">
<p style="color:#9ca3af;font-size:11px">本信件由 AutoTest 自動發出,請勿直接回覆。</p>
</body></html>"""
    text = (
        f"AutoTest 專案邀請\n\n{inviter} 邀請您加入專案「{project_name}」。\n\n"
        f"請登入 AutoTest 後到「設定 → 兌換邀請碼」貼上邀請碼:\n"
        f"  {invite_code}\n\n"
        f"或開啟連結一鍵兌換:\n  {redeem_url}\n\n"
        f"邀請碼將於 {expires_at:%Y-%m-%d %H:%M} (UTC) 過期。"
    )
    try:
        send_email_sync(
            db=db_sync, to=to, subject=subject,
            html_body=html, text_body=text,
            organization_id=organization_id,
        )
    except Exception as e:
        _invite_logger.warning("invite email failed (non-fatal): %s", e)


def _frontend_origin() -> str:
    """從 ALLOWED_ORIGINS 取第一個當 frontend host,組 redeem URL。"""
    import os
    allow = os.environ.get("ALLOWED_ORIGINS") or ""
    for o in allow.split(","):
        o = o.strip()
        if o.startswith("http"):
            return o.rstrip("/")
    return "http://localhost"


@router.post(
    "/projects/{project_id}/invites",
    response_model=ProjectInviteResponse,
    status_code=201,
    dependencies=[Depends(require_casbin(P.USER_MANAGE))],
    tags=["G · 專案"],
)
async def create_project_invite(
    project_id: str,
    payload: ProjectInviteCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    proj = await db.get(Project, project_id)
    proj = await _check_org_or_404(proj, user, db)

    email_lc = payload.invitee_email.lower().strip()

    # 已是 member?
    existing_mem = (await db.execute(
        select(ProjectMember).join(User, User.username == ProjectMember.username)
        .where(ProjectMember.project_id == project_id, func.lower(User.email) == email_lc)
    )).scalars().first()
    if existing_mem:
        raise HTTPException(409, "該 email 對應的使用者已是本專案成員")

    # 是否已有同 email 的 pending invite?(防垃圾發信)
    existing_inv = (await db.execute(
        select(ProjectInvite).where(
            ProjectInvite.project_id == project_id,
            func.lower(ProjectInvite.invitee_email) == email_lc,
            ProjectInvite.status == "pending",
        )
    )).scalars().first()
    if existing_inv:
        raise HTTPException(
            409,
            f"已有 pending 邀請寄給 {email_lc};請先撤銷舊邀請或等使用者兌換 / 過期",
        )

    if payload.role_id:
        role = await db.get(Role, payload.role_id)
        if not role:
            raise HTTPException(400, "指定的 role_id 不存在")

    invite = ProjectInvite(
        project_id=project_id,
        organization_id=proj.organization_id or user.organization_id,
        invitee_email=email_lc,
        role_id=payload.role_id,
        inviter_username=user.username,
        expires_at=ProjectInvite.default_expires_at(payload.expires_days),
    )
    db.add(invite)
    await db.flush()
    await db.refresh(invite)

    # 寄信(非同步呼叫 sync helper;失敗只 log,invite row 仍保留)
    try:
        from app.db.sync_session import SessionLocal
        with SessionLocal() as sync_db:
            redeem_url = f"{_frontend_origin()}/?invite_code={invite.invite_code}"
            _send_invite_email(
                sync_db,
                to=email_lc,
                project_name=proj.name,
                invite_code=invite.invite_code,
                expires_at=invite.expires_at,
                inviter=user.display_name or user.username,
                redeem_url=redeem_url,
                organization_id=invite.organization_id,
            )
    except Exception as e:
        _invite_logger.warning("invite email send failed: %s", e)

    return invite


@router.get(
    "/projects/{project_id}/invites",
    response_model=list[ProjectInviteResponse],
    dependencies=[Depends(require_casbin(P.USER_MANAGE))],
    tags=["G · 專案"],
)
async def list_project_invites(
    project_id: str,
    status: Optional[str] = None,   # pending / redeemed / expired / revoked
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    proj = await db.get(Project, project_id)
    await _check_org_or_404(proj, user, db)
    stmt = select(ProjectInvite).where(ProjectInvite.project_id == project_id)
    if status:
        stmt = stmt.where(ProjectInvite.status == status)
    stmt = stmt.order_by(desc(ProjectInvite.created_at))
    rows = (await db.execute(stmt)).scalars().all()
    return rows


@router.delete(
    "/projects/invites/{invite_id}",
    status_code=204,
    dependencies=[Depends(require_casbin(P.USER_MANAGE))],
    tags=["G · 專案"],
)
async def revoke_project_invite(
    invite_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    inv = await db.get(ProjectInvite, invite_id)
    if inv is None:
        raise HTTPException(404, "邀請不存在")
    proj = await db.get(Project, inv.project_id)
    await _check_org_or_404(proj, user, db)
    if inv.status != "pending":
        # 已 redeemed / 已 revoked / 已 expired 都 idempotent skip
        return
    inv.status = "revoked"
    await db.flush()


@router.post(
    "/projects/invites/redeem",
    response_model=ProjectInviteResponse,
    tags=["G · 專案"],
)
async def redeem_project_invite(
    payload: ProjectInviteRedeem,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """任何登入 user 都能呼叫(沒 require_casbin)。內部驗 email 完全相符。"""
    code = (payload.invite_code or "").strip()
    if not code:
        raise HTTPException(400, "invite_code 不能為空")
    inv = (await db.execute(
        select(ProjectInvite).where(ProjectInvite.invite_code == code)
    )).scalar_one_or_none()
    if inv is None:
        raise HTTPException(404, "邀請碼不存在")
    if inv.status == "redeemed":
        raise HTTPException(409, "此邀請碼已被使用過")
    if inv.status == "revoked":
        raise HTTPException(409, "此邀請碼已被撤銷")
    if inv.expires_at < datetime.utcnow():
        if inv.status == "pending":
            inv.status = "expired"
            await db.flush()
        raise HTTPException(409, "此邀請碼已過期")

    user_email = (user.email or "").lower().strip()
    if not user_email:
        raise HTTPException(
            403,
            "您的帳號沒有設定 email,請先到「個人設定 → 帳戶資訊」補上 email 後再兌換",
        )
    if user_email != inv.invitee_email.lower():
        raise HTTPException(
            403,
            f"此邀請碼是寄給 {inv.invitee_email},您目前登入的帳號 email 是 {user_email},不能兌換",
        )

    # 是否已是 member?(防重複)
    existing = (await db.execute(
        select(ProjectMember).where(
            ProjectMember.project_id == inv.project_id,
            ProjectMember.username == user.username,
        )
    )).scalars().first()
    if existing:
        if existing.status != "active":
            existing.status = "active"
        if inv.role_id:
            existing.role_id = inv.role_id
    else:
        db.add(ProjectMember(
            project_id=inv.project_id,
            username=user.username,
            role_id=inv.role_id,
            status="active",
        ))

    inv.status = "redeemed"
    inv.redeemed_at = datetime.utcnow()
    inv.redeemed_by_username = user.username
    await db.flush()
    await db.refresh(inv)
    return inv
