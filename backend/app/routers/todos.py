"""TodoItem REST endpoints — 首頁日曆 + 待辦清單。"""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import asc, desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common import Pagination
from app.database import get_db
from app.models.todo_item import TodoItem, TodoItemType, TodoPriority, TodoStatus
from app.schemas.settings import (
    TodoItemCreate,
    TodoItemResponse,
    TodoItemUpdate,
)

router = APIRouter()


def _resolve_status(val, default):
    if val is None:
        return default
    try:
        return TodoStatus(val)
    except ValueError:
        return default


def _resolve_priority(val, default):
    if val is None:
        return default
    try:
        return TodoPriority(val)
    except ValueError:
        return default


def _resolve_type(val, default):
    if val is None:
        return default
    try:
        return TodoItemType(val)
    except ValueError:
        return default


def _enrich(t: TodoItem) -> dict:
    """把 ORM 物件轉成 dict 並加上 is_overdue / days_to_due。"""
    is_overdue = False
    days_to_due = None
    if t.due_date and t.status not in (TodoStatus.DONE, TodoStatus.CANCELLED):
        try:
            d = date.fromisoformat(t.due_date)
            today = date.today()
            delta = (d - today).days
            days_to_due = delta
            is_overdue = delta < 0
        except (ValueError, TypeError):
            pass
    return {
        "id": t.id,
        "project_id": t.project_id,
        "title": t.title,
        "description": t.description,
        "due_date": t.due_date,
        "status": t.status.value if hasattr(t.status, "value") else str(t.status),
        "priority": t.priority.value if hasattr(t.priority, "value") else str(t.priority),
        "assignee": t.assignee,
        "related_entity_type": t.related_entity_type,
        "related_entity_id": t.related_entity_id,
        "item_type": t.item_type.value if hasattr(t.item_type, "value") else str(t.item_type),
        "parent_id": t.parent_id,
        "sprint_label": t.sprint_label,
        "completed_at": t.completed_at,
        "created_at": t.created_at,
        "updated_at": t.updated_at,
        "is_overdue": is_overdue,
        "days_to_due": days_to_due,
    }


@router.get("/todos", tags=["T · 待辦"])
async def list_todos(
    project_id: Optional[str] = Query(None),
    assignee: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    item_type: Optional[str] = Query(None, description="Epic / Story / Task / Bug / Spike"),
    parent_id: Optional[str] = Query(None, description="篩選某個父項下的子項"),
    sprint_label: Optional[str] = Query(None, description="Sprint label;傳 '__backlog__' 代表沒掛 sprint"),
    bucket: Optional[str] = Query(
        None,
        description="overdue / due_soon (≤3 天) / upcoming / done。覆蓋 status 過濾。",
    ),
    page: Pagination = Depends(Pagination.from_query),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(TodoItem).order_by(asc(TodoItem.due_date), desc(TodoItem.created_at))
    if project_id:
        stmt = stmt.where(TodoItem.project_id == project_id)
    if assignee:
        stmt = stmt.where(TodoItem.assignee == assignee)
    if status:
        stmt = stmt.where(TodoItem.status == TodoStatus(status))
    if item_type:
        try:
            stmt = stmt.where(TodoItem.item_type == TodoItemType(item_type))
        except ValueError:
            pass
    if parent_id:
        stmt = stmt.where(TodoItem.parent_id == parent_id)
    if sprint_label:
        if sprint_label == "__backlog__":
            stmt = stmt.where(TodoItem.sprint_label.is_(None))
        else:
            stmt = stmt.where(TodoItem.sprint_label == sprint_label)
    stmt = page.apply(stmt)
    rows = (await db.execute(stmt)).scalars().all()
    enriched = [_enrich(t) for t in rows]

    if bucket:
        if bucket == "overdue":
            enriched = [e for e in enriched if e["is_overdue"]]
        elif bucket == "due_soon":
            enriched = [
                e for e in enriched
                if e["days_to_due"] is not None and 0 <= e["days_to_due"] <= 3
                and e["status"] not in ("Done", "Cancelled")
            ]
        elif bucket == "upcoming":
            enriched = [
                e for e in enriched
                if e["days_to_due"] is not None and e["days_to_due"] > 3
                and e["status"] not in ("Done", "Cancelled")
            ]
        elif bucket == "done":
            enriched = [e for e in enriched if e["status"] in ("Done", "Cancelled")]
    return enriched


@router.get("/todos/summary", tags=["T · 待辦"])
async def todo_summary(
    project_id: Optional[str] = Query(None),
    assignee: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """首頁 KPI 卡片用:各 bucket 的數量。"""
    stmt = select(TodoItem)
    if project_id:
        stmt = stmt.where(TodoItem.project_id == project_id)
    if assignee:
        stmt = stmt.where(TodoItem.assignee == assignee)
    rows = (await db.execute(stmt)).scalars().all()
    enriched = [_enrich(t) for t in rows]
    overdue = sum(1 for e in enriched if e["is_overdue"])
    due_soon = sum(
        1 for e in enriched
        if e["days_to_due"] is not None and 0 <= e["days_to_due"] <= 3
        and e["status"] not in ("Done", "Cancelled")
    )
    todo = sum(1 for e in enriched if e["status"] == "Todo" and not e["is_overdue"])
    in_progress = sum(1 for e in enriched if e["status"] == "InProgress" and not e["is_overdue"])
    done = sum(1 for e in enriched if e["status"] == "Done")
    return {
        "overdue": overdue,
        "due_soon": due_soon,
        "todo": todo,
        "in_progress": in_progress,
        "done": done,
        "total_active": sum(1 for e in enriched if e["status"] not in ("Done", "Cancelled")),
    }


@router.get("/todos/tree", tags=["T · 待辦"])
async def todo_tree(
    project_id: Optional[str] = Query(None),
    assignee: Optional[str] = Query(None),
    sprint_label: Optional[str] = Query(None),
    include_done: bool = Query(False, description="是否包含已完成項目"),
    db: AsyncSession = Depends(get_db),
):
    """側邊欄 / Backlog view 用:回傳階層化的待辦樹。

    結構:
        [
          { ...Story/Epic..., children: [Task, Task, Bug] },
          { ...孤立 Bug/Spike/Task...(沒有 parent) },
          ...
        ]
    """
    stmt = select(TodoItem).order_by(
        asc(TodoItem.item_type),
        asc(TodoItem.due_date),
        desc(TodoItem.created_at),
    )
    if project_id:
        stmt = stmt.where(TodoItem.project_id == project_id)
    if assignee:
        stmt = stmt.where(TodoItem.assignee == assignee)
    if sprint_label:
        if sprint_label == "__backlog__":
            stmt = stmt.where(TodoItem.sprint_label.is_(None))
        else:
            stmt = stmt.where(TodoItem.sprint_label == sprint_label)
    if not include_done:
        stmt = stmt.where(TodoItem.status.notin_([TodoStatus.DONE, TodoStatus.CANCELLED]))

    rows = (await db.execute(stmt)).scalars().all()
    items = [_enrich(t) for t in rows]
    by_id = {it["id"]: it for it in items}
    for it in items:
        it["children"] = []

    roots = []
    for it in items:
        pid = it.get("parent_id")
        if pid and pid in by_id:
            by_id[pid]["children"].append(it)
        else:
            roots.append(it)
    # 顯示順序:Epic > Story > Task > Bug > Spike,然後依到期日
    type_order = {"Epic": 0, "Story": 1, "Task": 2, "Bug": 3, "Spike": 4}
    roots.sort(key=lambda x: (
        type_order.get(x.get("item_type"), 99),
        x.get("due_date") or "9999-99-99",
    ))
    return roots


@router.post("/todos", response_model=TodoItemResponse, status_code=201, tags=["T · 待辦"])
async def create_todo(payload: TodoItemCreate, db: AsyncSession = Depends(get_db)):
    t = TodoItem(
        project_id=payload.project_id,
        title=payload.title,
        description=payload.description,
        due_date=payload.due_date,
        status=_resolve_status(payload.status, TodoStatus.TODO),
        priority=_resolve_priority(payload.priority, TodoPriority.P2),
        assignee=payload.assignee,
        related_entity_type=payload.related_entity_type,
        related_entity_id=payload.related_entity_id,
        item_type=_resolve_type(payload.item_type, TodoItemType.TASK),
        parent_id=payload.parent_id,
        sprint_label=payload.sprint_label,
    )
    db.add(t)
    await db.flush()
    await db.refresh(t)
    return _enrich(t)


@router.get("/todos/{todo_id}", response_model=TodoItemResponse, tags=["T · 待辦"])
async def get_todo(todo_id: str, db: AsyncSession = Depends(get_db)):
    t = await db.get(TodoItem, todo_id)
    if not t:
        raise HTTPException(404, "Todo not found")
    return _enrich(t)


@router.put("/todos/{todo_id}", response_model=TodoItemResponse, tags=["T · 待辦"])
async def update_todo(todo_id: str, payload: TodoItemUpdate, db: AsyncSession = Depends(get_db)):
    t = await db.get(TodoItem, todo_id)
    if not t:
        raise HTTPException(404, "Todo not found")
    data = payload.model_dump(exclude_unset=True)
    for key, val in data.items():
        if key == "status" and val is not None:
            new_st = _resolve_status(val, t.status)
            t.status = new_st
            if new_st in (TodoStatus.DONE, TodoStatus.CANCELLED) and t.completed_at is None:
                t.completed_at = datetime.utcnow()
            elif new_st not in (TodoStatus.DONE, TodoStatus.CANCELLED):
                t.completed_at = None
        elif key == "priority" and val is not None:
            t.priority = _resolve_priority(val, t.priority)
        elif key == "item_type" and val is not None:
            t.item_type = _resolve_type(val, t.item_type)
        else:
            setattr(t, key, val)
    await db.flush()
    await db.refresh(t)
    return _enrich(t)


@router.delete("/todos/{todo_id}", status_code=204, tags=["T · 待辦"])
async def delete_todo(todo_id: str, db: AsyncSession = Depends(get_db)):
    t = await db.get(TodoItem, todo_id)
    if not t:
        raise HTTPException(404, "Todo not found")
    await db.delete(t)
    await db.flush()
