from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.auth.scope import ensure_project_in_scope, ensure_project_writable
from app.database import get_db
from app.models.tree_node import LevelType, TreeNode
from app.models.user import User
from app.schemas.tree_node import TreeNodeCreate, TreeNodePartialUpdate
from app.services import entity_version_service as evs
from app.services.tree_service import get_expected_level, recursive_delete

router = APIRouter()


# 4. POST /api/v1/nodes
@router.post("/nodes", status_code=201)
async def create_node(
    payload: TreeNodeCreate,
    from_ai: bool = False,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """新增節點;後端自動計算並驗證 level_type(防呆層級限制)。

    AB 表設計:葉節點(testcase)走 entity_version snapshot;非葉節點(feature/
    page/scenario 等容器)目前先不做版本管理(沒有實際內容)。
    """
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

    # AB 表 snapshot:只對 testcase 葉節點建立版本歷史
    if expected_level == LevelType.TESTCASE:
        status = evs.CONTENT_STATUS_AI_DRAFT if from_ai else evs.CONTENT_STATUS_PENDING
        source = evs.CHANGE_SOURCE_AI if from_ai else evs.CHANGE_SOURCE_HUMAN
        await evs.snapshot(
            db,
            entity_type="testcase",
            entity=node,
            source=source,
            status=status,
            by=user.username,
        )

    return {
        "id": node.id,
        "project_id": node.project_id,
        "parent_id": node.parent_id,
        "level_type": node.level_type,
        "name": node.name,
        "sort_order": node.sort_order,
        "content_status": node.content_status,
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
    changed = False
    if payload.name is not None and payload.name != node.name:
        node.name = payload.name
        changed = True
    if payload.sort_order is not None and payload.sort_order != node.sort_order:
        node.sort_order = payload.sort_order
        changed = True
    await db.flush()
    # 任何 testcase 葉節點的更新都記一筆 pending_review snapshot(由人工編輯觸發)
    if changed and node.level_type == LevelType.TESTCASE:
        await evs.snapshot(
            db,
            entity_type="testcase",
            entity=node,
            source=evs.CHANGE_SOURCE_HUMAN,
            status=evs.CONTENT_STATUS_PENDING,
            by=user.username,
        )
    return {"id": node.id, "name": node.name, "sort_order": node.sort_order, "content_status": node.content_status}


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
