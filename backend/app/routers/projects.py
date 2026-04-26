from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.database import get_db
from app.models.project import Project
from app.models.tree_node import TreeNode
from app.models.user import User
from app.schemas.project import ProjectCreate, ProjectResponse, ProjectUpdate
from app.services.tree_service import build_tree

router = APIRouter()


def _scope_filter(stmt, user: User):
    """以 organization_id 過濾；superuser 看得到全部，普通使用者只看自己的 org。"""
    if user.is_superuser:
        return stmt
    return stmt.where(Project.organization_id == user.organization_id)


# 1. GET /api/projects
@router.get("/projects", response_model=list[ProjectResponse])
async def list_projects(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(Project).order_by(Project.created_at.desc())
    stmt = _scope_filter(stmt, user)
    result = await db.execute(stmt)
    return result.scalars().all()


# 2. POST /api/projects
@router.post("/projects", response_model=ProjectResponse, status_code=201)
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
        status=payload.status or "Active",
        start_date=payload.start_date,
        target_date=payload.target_date,
        tags=payload.tags,
    )
    db.add(project)
    await db.flush()
    await db.refresh(project)
    return project


def _check_org_or_404(proj: Optional[Project], user: User) -> Project:
    """共用：找不到或 org 不對都回 404（不洩漏「跨 org 存在」資訊）。"""
    if proj is None:
        raise HTTPException(status_code=404, detail="Project not found")
    if not user.is_superuser and proj.organization_id != user.organization_id:
        raise HTTPException(status_code=404, detail="Project not found")
    return proj


# 3. GET /api/projects/{projectId}/tree
@router.get("/projects/{project_id}/tree")
async def get_project_tree(
    project_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """一次撈出整棵樹，回傳巢狀 JSON（核心 API）。"""
    proj = await db.get(Project, project_id)
    _check_org_or_404(proj, user)

    result = await db.execute(
        select(TreeNode)
        .where(TreeNode.project_id == project_id)
        .order_by(TreeNode.sort_order)
    )
    nodes = result.scalars().all()
    return build_tree(nodes, parent_id=None)


# 4. DELETE /api/projects/{projectId}
@router.delete("/projects/{project_id}", status_code=204)
async def delete_project(
    project_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """刪除專案（連同樹狀節點、測試案例、執行報告等一併由 DB cascade 刪除）。"""
    proj = await db.get(Project, project_id)
    proj = _check_org_or_404(proj, user)
    await db.delete(proj)
    await db.commit()
    return None


# 5. PUT /api/projects/{projectId}
@router.put("/projects/{project_id}", response_model=ProjectResponse)
async def update_project(
    project_id: str,
    payload: ProjectUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """更新測試專案的欄位（部分更新，未提供的欄位保留）。"""
    proj = await db.get(Project, project_id)
    proj = _check_org_or_404(proj, user)
    data = payload.model_dump(exclude_unset=True)
    if "name" in data:
        name = (data["name"] or "").strip()
        if not name:
            raise HTTPException(status_code=400, detail="Project name cannot be empty")
        proj.name = name
    for key in ("description", "owner", "status", "start_date", "target_date", "tags"):
        if key in data:
            setattr(proj, key, data[key])
    await db.flush()
    await db.refresh(proj)
    return proj


# 6. GET /api/projects/{projectId} — 單一測試專案詳情
@router.get("/projects/{project_id}", response_model=ProjectResponse)
async def get_project(
    project_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    proj = await db.get(Project, project_id)
    return _check_org_or_404(proj, user)
