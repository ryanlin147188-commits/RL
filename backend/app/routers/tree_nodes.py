from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.auth.scope import ensure_project_in_scope, ensure_project_writable
from app.database import get_db
from app.models.tree_node import TreeNode
from app.models.user import User
from app.schemas.tree_node import TreeNodeCreate, TreeNodePartialUpdate
from app.services.tree_service import get_expected_level, recursive_delete

router = APIRouter()


# 4. POST /api/v1/nodes
@router.post("/nodes", status_code=201)
async def create_node(
    payload: TreeNodeCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """新增節點;後端自動計算並驗證 level_type(防呆層級限制)。"""
    await ensure_project_writable(db, payload.project_id, user)
    expected_level = await get_expected_level(db, payload.project_id, payload.parent_id)

    node = TreeNode(
        project_id=payload.project_id,
        parent_id=payload.parent_id,
        level_type=expected_level,
        name=payload.name,
        sort_order=payload.sort_order,
    )
    db.add(node)
    await db.flush()
    await db.refresh(node)

    return {
        "id": node.id,
        "project_id": node.project_id,
        "parent_id": node.parent_id,
        "level_type": node.level_type,
        "name": node.name,
        "sort_order": node.sort_order,
    }


# 5. PATCH /api/v1/nodes/{id}
@router.patch("/nodes/{node_id}")
async def update_node(
    node_id: str,
    payload: TreeNodePartialUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """部分更新:可單獨修改 name 或 sort_order。"""
    node = await db.get(TreeNode, node_id)
    await ensure_project_in_scope(
        db, node.project_id if node else None, user, not_found_detail="Node not found"
    )
    if payload.name is not None:
        node.name = payload.name
    if payload.sort_order is not None:
        node.sort_order = payload.sort_order
    await db.flush()
    return {"id": node.id, "name": node.name, "sort_order": node.sort_order}


# 6. DELETE /api/v1/nodes/{id}
@router.delete("/nodes/{node_id}", status_code=204)
async def delete_node(
    node_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """刪除節點及其所有子孫(DB 層 CASCADE 處理)。"""
    node = await db.get(TreeNode, node_id)
    await ensure_project_in_scope(
        db, node.project_id if node else None, user, not_found_detail="Node not found"
    )
    await recursive_delete(db, node_id)
