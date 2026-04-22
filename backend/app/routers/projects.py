from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.project import Project
from app.models.tree_node import TreeNode
from app.schemas.project import ProjectCreate, ProjectResponse
from app.services.tree_service import build_tree

router = APIRouter()


# 1. GET /api/projects
@router.get("/projects", response_model=list[ProjectResponse])
async def list_projects(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Project).order_by(Project.created_at.desc()))
    return result.scalars().all()


# 2. POST /api/projects
@router.post("/projects", response_model=ProjectResponse, status_code=201)
async def create_project(payload: ProjectCreate, db: AsyncSession = Depends(get_db)):
    project = Project(name=payload.name)
    db.add(project)
    await db.flush()
    await db.refresh(project)
    return project


# 3. GET /api/projects/{projectId}/tree
@router.get("/projects/{project_id}/tree")
async def get_project_tree(project_id: str, db: AsyncSession = Depends(get_db)):
    """一次撈出整棵樹，回傳巢狀 JSON（核心 API）。"""
    proj = await db.get(Project, project_id)
    if proj is None:
        raise HTTPException(status_code=404, detail="Project not found")

    result = await db.execute(
        select(TreeNode)
        .where(TreeNode.project_id == project_id)
        .order_by(TreeNode.sort_order)
    )
    nodes = result.scalars().all()
    return build_tree(nodes, parent_id=None)


# 4. DELETE /api/projects/{projectId}
@router.delete("/projects/{project_id}", status_code=204)
async def delete_project(project_id: str, db: AsyncSession = Depends(get_db)):
    """刪除專案（連同樹狀節點、測試案例、執行報告等一併由 DB cascade 刪除）。"""
    proj = await db.get(Project, project_id)
    if proj is None:
        raise HTTPException(status_code=404, detail="Project not found")
    await db.delete(proj)
    await db.commit()
    return None


# 5. PUT /api/projects/{projectId}
@router.put("/projects/{project_id}", response_model=ProjectResponse)
async def update_project(
    project_id: str, payload: ProjectCreate, db: AsyncSession = Depends(get_db)
):
    """更新專案名稱。"""
    proj = await db.get(Project, project_id)
    if proj is None:
        raise HTTPException(status_code=404, detail="Project not found")
    name = (payload.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Project name cannot be empty")
    proj.name = name
    await db.flush()
    await db.refresh(proj)
    return proj
