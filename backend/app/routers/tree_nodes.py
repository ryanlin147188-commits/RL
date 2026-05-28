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


# 6.5 POST /api/v1/nodes/{id}/move — v1.3.x:真正搬家(改 parent_id)
@router.post("/nodes/{node_id}/move")
async def move_node(
    node_id: str,
    payload: dict,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """把 node 搬到 new_parent_id 底下。

    payload: ``{"new_parent_id": "<uuid>" | null}``
    new_parent_id=null = 搬到 project 根層(那 node 必須是 Feature 才合法)。

    驗證:
    * node 與 new_parent 必須在同一 project(跨 project move 太複雜,先擋)
    * hierarchy 規則:new_parent 之下的 expected level **必須等於** node 自己的
      level_type(例如 Page node 只能搬到 PLATFORM 之下;Feature 只能搬到 root)
    * 不能搬到自己或自己的子孫底下(cycle 防護)
    """
    node = await db.get(TreeNode, node_id)
    if node is None:
        raise HTTPException(404, "node not found")
    await ensure_project_writable(db, node.project_id, user)

    new_parent_id = (payload or {}).get("new_parent_id") or None

    # 不能搬到自己
    if new_parent_id == node_id:
        raise HTTPException(400, "不能把 node 搬到自己底下")

    # 同 project 限制 + new_parent 必須存在(若給)
    if new_parent_id is not None:
        new_parent = await db.get(TreeNode, new_parent_id)
        if new_parent is None:
            raise HTTPException(404, "new_parent_id 不存在")
        if new_parent.project_id != node.project_id:
            raise HTTPException(400, "跨 project move 不支援;new_parent 必須在同一 project")
        # cycle 防護:走 ancestors 鏈,若遇到 node_id 就拒
        cursor = new_parent
        while cursor is not None:
            if cursor.id == node_id:
                raise HTTPException(400, "不能搬到自己的子孫節點底下(cycle)")
            if cursor.parent_id is None:
                break
            cursor = await db.get(TreeNode, cursor.parent_id)

    # Hierarchy 規則:new_parent 之下的 expected level 必須等於 node.level_type
    expected_level = await get_expected_level(db, node.project_id, new_parent_id)
    if expected_level != node.level_type:
        raise HTTPException(
            400,
            f"層級不符:node 是 {node.level_type.value},但 new_parent 底下只能放"
            f" {expected_level.value if expected_level else 'None'}",
        )

    old_parent = node.parent_id
    node.parent_id = new_parent_id
    await db.flush()
    await db.refresh(node)

    # Testcase 葉節點 — 結構變更也記一次 snapshot
    if node.level_type == LevelType.TESTCASE:
        try:
            await evs.snapshot(
                db,
                entity_type="testcase",
                entity=node,
                source=evs.CHANGE_SOURCE_HUMAN,
                status=evs.CONTENT_STATUS_PENDING,
                by=user.username,
            )
        except Exception:  # noqa: BLE001
            pass

    return {
        "id": node.id,
        "project_id": node.project_id,
        "old_parent_id": old_parent,
        "new_parent_id": new_parent_id,
        "level_type": node.level_type.value,
        "name": node.name,
    }


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
