"""TodoLink REST endpoints — Backlog 跨實體連結 CRUD + 反向查詢。

功能:
- 任一 TodoItem 連到任一目標實體(N:M)
- 反向查詢:某實體被哪些 Todo 連到(給缺陷卡片 / 需求清單徽章用)
- 批次反查:給 RTM 矩陣 / 看板畫面用,1 round-trip 拿全部徽章資料
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.database import get_db
from app.models.defect import Defect
from app.models.project import Project
from app.models.requirement import Requirement
from app.models.test_document import TestDocument
from app.models.test_milestone import TestMilestone
from app.models.test_plan import TestPlan
from app.models.test_round import TestRound
from app.models.todo_item import TodoItem
from app.models.todo_link import ALLOWED_TARGET_TYPES, TodoLink
from app.models.tree_node import LevelType, TreeNode
from app.models.user import User
from app.models.wbs_item import WbsItem
from app.schemas.todo_link import (
    TodoLinkCreate,
    TodoLinkResponse,
    TodoSummaryForLink,
)

router = APIRouter()


# ── 各 target_type 對應的 ORM model + 顯示用欄位 ─────────────────────
# 統一介面:給定 target_type + target_id,可以撈 (id, title, code) 三件套
_TARGET_REGISTRY = {
    "requirement": (Requirement, "title", "code"),
    "defect": (Defect, "title", "code"),
    "test_plan": (TestPlan, "name", None),
    "test_round": (TestRound, "name", None),
    "test_milestone": (TestMilestone, "name", None),
    "wbs": (WbsItem, "name", None),
    "test_document": (TestDocument, "title", None),
    "project": (Project, "name", None),
    # testcase 特殊:tree_nodes 有 level_type 約束
}


async def _validate_target(
    db: AsyncSession, target_type: str, target_id: str
) -> tuple[Optional[str], Optional[str]]:
    """驗證目標存在;回 (title, code)。失敗丟 HTTPException。"""
    if target_type not in ALLOWED_TARGET_TYPES:
        raise HTTPException(400, f"target_type 不支援:{target_type}")

    if target_type == "testcase":
        node = await db.get(TreeNode, target_id)
        if not node:
            raise HTTPException(404, f"找不到 testcase:{target_id}")
        if node.level_type != LevelType.TESTCASE:
            raise HTTPException(400, "target 必須是 level_type=TESTCASE 的 tree_node")
        return (node.name, None)

    spec = _TARGET_REGISTRY.get(target_type)
    if not spec:
        raise HTTPException(400, f"target_type 對應未實作:{target_type}")
    Model, title_attr, code_attr = spec
    obj = await db.get(Model, target_id)
    if not obj:
        raise HTTPException(404, f"找不到 {target_type}:{target_id}")
    title = getattr(obj, title_attr, None)
    code = getattr(obj, code_attr, None) if code_attr else None
    return (title, code)


async def _enrich_link(db: AsyncSession, link: TodoLink) -> dict:
    title, code = await _validate_target(db, link.target_type, link.target_id)
    return {
        "id": link.id,
        "todo_id": link.todo_id,
        "organization_id": link.organization_id,
        "target_type": link.target_type,
        "target_id": link.target_id,
        "link_kind": link.link_kind,
        "note": link.note,
        "created_at": link.created_at,
        "created_by": link.created_by,
        "target_title": title,
        "target_code": code,
    }


# ── 1) 列出某 Todo 的 outbound links ────────────────────────────────
@router.get(
    "/todos/{todo_id}/links",
    response_model=list[TodoLinkResponse],
    tags=["T · 待辦"],
)
async def list_todo_links(
    todo_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    todo = await db.get(TodoItem, todo_id)
    if not todo:
        raise HTTPException(404, "Todo not found")
    if not user.is_superuser and todo.organization_id != user.organization_id:
        raise HTTPException(404, "Todo not found")

    rows = (
        await db.execute(
            select(TodoLink).where(TodoLink.todo_id == todo_id).order_by(TodoLink.created_at)
        )
    ).scalars().all()
    return [await _enrich_link(db, l) for l in rows]


# ── 2) 新增連結 ────────────────────────────────────────────────────
@router.post(
    "/todos/{todo_id}/links",
    response_model=TodoLinkResponse,
    status_code=201,
    tags=["T · 待辦"],
)
async def create_todo_link(
    todo_id: str,
    payload: TodoLinkCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    todo = await db.get(TodoItem, todo_id)
    if not todo:
        raise HTTPException(404, "Todo not found")
    if not user.is_superuser and todo.organization_id != user.organization_id:
        raise HTTPException(404, "Todo not found")

    # 驗證目標
    await _validate_target(db, payload.target_type, payload.target_id)

    # 重複檢查
    dup = (
        await db.execute(
            select(TodoLink).where(
                TodoLink.todo_id == todo_id,
                TodoLink.target_type == payload.target_type,
                TodoLink.target_id == payload.target_id,
                TodoLink.link_kind == (payload.link_kind or "relates_to"),
            )
        )
    ).scalar_one_or_none()
    if dup:
        raise HTTPException(409, "Link already exists")

    link = TodoLink(
        organization_id=user.organization_id or todo.organization_id,
        todo_id=todo_id,
        target_type=payload.target_type,
        target_id=payload.target_id,
        link_kind=payload.link_kind or "relates_to",
        note=payload.note,
        created_by=user.username,
    )
    db.add(link)
    await db.flush()
    await db.refresh(link)
    return await _enrich_link(db, link)


# ── 3) 刪除連結 ────────────────────────────────────────────────────
@router.delete(
    "/todos/{todo_id}/links/{link_id}",
    status_code=204,
    tags=["T · 待辦"],
)
async def delete_todo_link(
    todo_id: str,
    link_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    link = await db.get(TodoLink, link_id)
    if not link or link.todo_id != todo_id:
        raise HTTPException(404, "Link not found")
    if not user.is_superuser and link.organization_id != user.organization_id:
        raise HTTPException(404, "Link not found")
    await db.delete(link)
    await db.flush()


# ── 4) 反向查詢:某實體被哪些 Todo 連到 ─────────────────────────────
@router.get(
    "/links/by-target",
    response_model=list[TodoSummaryForLink],
    tags=["T · 待辦"],
)
async def list_todos_by_target(
    target_type: str = Query(...),
    target_id: str = Query(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if target_type not in ALLOWED_TARGET_TYPES:
        raise HTTPException(400, f"target_type 不支援:{target_type}")

    stmt = (
        select(TodoLink, TodoItem)
        .join(TodoItem, TodoItem.id == TodoLink.todo_id)
        .where(
            TodoLink.target_type == target_type,
            TodoLink.target_id == target_id,
        )
    )
    if not user.is_superuser:
        stmt = stmt.where(TodoLink.organization_id == user.organization_id)
    rows = (await db.execute(stmt)).all()

    return [
        TodoSummaryForLink(
            id=t.id,
            title=t.title,
            item_type=t.item_type.value if hasattr(t.item_type, "value") else str(t.item_type),
            status=t.status.value if hasattr(t.status, "value") else str(t.status),
            priority=t.priority.value if hasattr(t.priority, "value") else str(t.priority),
            assignee=t.assignee,
            link_kind=l.link_kind,
        )
        for (l, t) in rows
    ]


# ── 5) 批次反向查詢:整個專案某 type 一次拿完 ────────────────────────
# 給看板 / 需求清單 / RTM 矩陣畫徽章用,1 round-trip 拿全部 link 資訊。
@router.get(
    "/links/by-target/batch",
    tags=["T · 待辦"],
)
async def batch_links_by_target(
    target_type: str = Query(...),
    project_id: Optional[str] = Query(None, description="限定 Todo 的 project_id"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """回 `{target_id: [{id, title, item_type, status, priority, link_kind}, ...]}`。"""
    if target_type not in ALLOWED_TARGET_TYPES:
        raise HTTPException(400, f"target_type 不支援:{target_type}")

    stmt = (
        select(TodoLink, TodoItem)
        .join(TodoItem, TodoItem.id == TodoLink.todo_id)
        .where(TodoLink.target_type == target_type)
    )
    if not user.is_superuser:
        stmt = stmt.where(TodoLink.organization_id == user.organization_id)
    if project_id:
        stmt = stmt.where(TodoItem.project_id == project_id)
    rows = (await db.execute(stmt)).all()

    grouped: dict[str, list[dict]] = {}
    for (l, t) in rows:
        grouped.setdefault(l.target_id, []).append({
            "id": t.id,
            "title": t.title,
            "item_type": t.item_type.value if hasattr(t.item_type, "value") else str(t.item_type),
            "status": t.status.value if hasattr(t.status, "value") else str(t.status),
            "priority": t.priority.value if hasattr(t.priority, "value") else str(t.priority),
            "assignee": t.assignee,
            "link_kind": l.link_kind,
        })
    return grouped
