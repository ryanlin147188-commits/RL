"""TodoItem REST endpoints — 首頁日曆 + 待辦清單。"""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import asc, desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.auth.project_membership import ensure_project_member
from app.common import Pagination
from app.database import get_db
from app.models.group import Group
from app.models.notification import Notification
from app.models.todo_item import TodoItem, TodoItemType, TodoPriority, TodoStatus
from app.models.user import User
from app.schemas.settings import (
    TodoAssignRequest,
    TodoItemCreate,
    TodoItemResponse,
    TodoItemUpdate,
)
from app.services.group_resolver import resolve_group_members

router = APIRouter()


def _scope_todo(stmt, user: User):
    """以 organization_id 過濾 TodoItem;superuser 不限制。"""
    if user.is_superuser:
        return stmt
    return stmt.where(TodoItem.organization_id == user.organization_id)


def _check_todo_scope(t: Optional[TodoItem], user: User) -> TodoItem:
    if t is None:
        raise HTTPException(404, "Todo not found")
    if not user.is_superuser and t.organization_id != user.organization_id:
        raise HTTPException(404, "Todo not found")
    return t


async def _notify_assignment(
    db: AsyncSession,
    todo: TodoItem,
    actor: User,
) -> None:
    """指派 / 轉派時推站內通知。

    - assigned_to_type='user' → 推給該 username(自己指派自己時跳過)
    - assigned_to_type='group' → 遞迴展開群組所有成員(含巢狀子群組);發送者自己跳過
    取消指派(assigned_to=None)在呼叫端就 return,不會走到這裡。
    """
    if not todo.assigned_to:
        return
    targets: set[str] = set()
    body_who = ""
    if todo.assigned_to_type == "group":
        g = await db.get(Group, todo.assigned_to)
        if g is None:
            return
        targets = await resolve_group_members(db, g.id)
        body_who = f"群組「{g.name}」"
    else:
        targets = {todo.assigned_to}
        body_who = "你"
    targets.discard(actor.username)  # 不打擾自己
    if not targets:
        return
    actor_label = actor.display_name or actor.username
    title = f"待辦被指派給{body_who}:{todo.title}"
    body = f"{actor_label} 將「{todo.title}」指派給{body_who}"
    for username in targets:
        db.add(Notification(
            organization_id=todo.organization_id,
            recipient=username,
            title=title,
            body=body,
            level="info",
            event_key="todo.assigned",
            link=f"/#todo/{todo.id}",
            related_entity_type="todo",
            related_entity_id=todo.id,
        ))


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
    if t.due_date and t.status not in (TodoStatus.VERIFIED, TodoStatus.CLOSED):
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
        # API 對外保留 assignee/assignee_type 命名(避免 break 已有 client)
        "assignee": t.assigned_to,
        "assignee_type": t.assigned_to_type or "user",
        "assigned_by": t.assigned_by,
        "assigned_at": t.assigned_at,
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


@router.get(
    "/todos",
    tags=["T · 待辦"],
    dependencies=[Depends(ensure_project_member)],
)
async def list_todos(
    project_id: Optional[str] = Query(None),
    assignee: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    item_type: Optional[str] = Query(None, description="Feature / Task / Bug / Spike"),
    parent_id: Optional[str] = Query(None, description="篩選某個父項下的子項"),
    sprint_label: Optional[str] = Query(None, description="Sprint label;傳 '__backlog__' 代表沒掛 sprint"),
    bucket: Optional[str] = Query(
        None,
        description="overdue / due_soon (≤3 天) / upcoming / done。覆蓋 status 過濾。",
    ),
    page: Pagination = Depends(Pagination.from_query),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(TodoItem).order_by(asc(TodoItem.due_date), desc(TodoItem.created_at))
    stmt = _scope_todo(stmt, user)
    if project_id:
        stmt = stmt.where(TodoItem.project_id == project_id)
    if assignee:
        stmt = stmt.where(TodoItem.assigned_to == assignee)
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
                and e["status"] not in ("Verified", "Closed")
            ]
        elif bucket == "upcoming":
            enriched = [
                e for e in enriched
                if e["days_to_due"] is not None and e["days_to_due"] > 3
                and e["status"] not in ("Verified", "Closed")
            ]
        elif bucket == "done":
            enriched = [e for e in enriched if e["status"] in ("Verified", "Closed")]
    return enriched


@router.get(
    "/todos/summary",
    tags=["T · 待辦"],
    dependencies=[Depends(ensure_project_member)],
)
async def todo_summary(
    project_id: Optional[str] = Query(None),
    assignee: Optional[str] = Query(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """首頁 KPI 卡片用:各 bucket 的數量。"""
    stmt = select(TodoItem)
    stmt = _scope_todo(stmt, user)
    if project_id:
        stmt = stmt.where(TodoItem.project_id == project_id)
    if assignee:
        stmt = stmt.where(TodoItem.assigned_to == assignee)
    rows = (await db.execute(stmt)).scalars().all()
    enriched = [_enrich(t) for t in rows]
    overdue = sum(1 for e in enriched if e["is_overdue"])
    due_soon = sum(
        1 for e in enriched
        if e["days_to_due"] is not None and 0 <= e["days_to_due"] <= 3
        and e["status"] not in ("Verified", "Closed")
    )
    todo = sum(1 for e in enriched if e["status"] in ("New", "Assigned") and not e["is_overdue"])
    in_progress = sum(1 for e in enriched if e["status"] == "InProgress" and not e["is_overdue"])
    done = sum(1 for e in enriched if e["status"] in ("Verified", "Closed"))
    return {
        "overdue": overdue,
        "due_soon": due_soon,
        "todo": todo,
        "in_progress": in_progress,
        "done": done,
        "total_active": sum(1 for e in enriched if e["status"] not in ("Verified", "Closed")),
    }


@router.get(
    "/todos/tree",
    tags=["T · 待辦"],
    dependencies=[Depends(ensure_project_member)],
)
async def todo_tree(
    project_id: Optional[str] = Query(None),
    assignee: Optional[str] = Query(None),
    sprint_label: Optional[str] = Query(None),
    include_done: bool = Query(False, description="是否包含已完成項目"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """側邊欄 / Backlog view 用:回傳階層化的待辦樹。

    結構:
        [
          { ...Feature..., children: [Task, Task, Bug] },
          { ...孤立 Bug/Spike/Task...(沒有 parent) },
          ...
        ]
    """
    stmt = select(TodoItem).order_by(
        asc(TodoItem.item_type),
        asc(TodoItem.due_date),
        desc(TodoItem.created_at),
    )
    stmt = _scope_todo(stmt, user)
    if project_id:
        stmt = stmt.where(TodoItem.project_id == project_id)
    if assignee:
        stmt = stmt.where(TodoItem.assigned_to == assignee)
    if sprint_label:
        if sprint_label == "__backlog__":
            stmt = stmt.where(TodoItem.sprint_label.is_(None))
        else:
            stmt = stmt.where(TodoItem.sprint_label == sprint_label)
    if not include_done:
        stmt = stmt.where(TodoItem.status.notin_([TodoStatus.VERIFIED, TodoStatus.CLOSED]))

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
    # 顯示順序:Feature > Task > Bug > Spike,然後依到期日
    type_order = {"Feature": 0, "Task": 1, "Bug": 2, "Spike": 3}
    roots.sort(key=lambda x: (
        type_order.get(x.get("item_type"), 99),
        x.get("due_date") or "9999-99-99",
    ))
    return roots


@router.post("/todos", response_model=TodoItemResponse, status_code=201, tags=["T · 待辦"])
async def create_todo(
    payload: TodoItemCreate,
    from_ai: bool = False,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    now = datetime.utcnow() if payload.assignee else None
    t = TodoItem(
        project_id=payload.project_id,
        organization_id=user.organization_id,
        title=payload.title,
        description=payload.description,
        due_date=payload.due_date,
        status=_resolve_status(payload.status, TodoStatus.NEW),
        priority=_resolve_priority(payload.priority, TodoPriority.P2),
        assigned_to=payload.assignee,
        assigned_to_type=(payload.assignee_type or "user") if payload.assignee else "user",
        assigned_by=user.username if payload.assignee else None,
        assigned_at=now,
        related_entity_type=payload.related_entity_type,
        related_entity_id=payload.related_entity_id,
        item_type=_resolve_type(payload.item_type, TodoItemType.TASK),
        parent_id=payload.parent_id,
        sprint_label=payload.sprint_label,
    )
    db.add(t)
    await db.flush()
    if payload.assignee:
        await _notify_assignment(db, t, user)
        await db.flush()
    await db.refresh(t)
    from app.services import entity_version_service as evs
    status_v = evs.CONTENT_STATUS_AI_DRAFT if from_ai else evs.CONTENT_STATUS_PENDING
    source_v = evs.CHANGE_SOURCE_AI if from_ai else evs.CHANGE_SOURCE_HUMAN
    await evs.snapshot(
        db, entity_type="todo", entity=t,
        source=source_v, status=status_v, by=user.username,
    )
    return _enrich(t)


@router.get("/todos/{todo_id}", response_model=TodoItemResponse, tags=["T · 待辦"])
async def get_todo(
    todo_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    t = await db.get(TodoItem, todo_id)
    _check_todo_scope(t, user)
    return _enrich(t)


@router.put("/todos/{todo_id}", response_model=TodoItemResponse, tags=["T · 待辦"])
async def update_todo(
    todo_id: str,
    payload: TodoItemUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    t = await db.get(TodoItem, todo_id)
    _check_todo_scope(t, user)
    data = payload.model_dump(exclude_unset=True)
    # API 對外保留 assignee/assignee_type 欄位名,內部欄位是 assigned_to/_type
    # (D-1 重命名後保持向後相容)
    _PAYLOAD_KEY_MAP = {"assignee": "assigned_to", "assignee_type": "assigned_to_type"}
    # 偵測指派變更:assignee 或 assignee_type 任一變動就重設 audit + 推通知
    prev_assignee = t.assigned_to
    prev_type = t.assigned_to_type or "user"
    assignment_changed = False
    for key, val in data.items():
        if key == "status" and val is not None:
            new_st = _resolve_status(val, t.status)
            t.status = new_st
            if new_st in (TodoStatus.VERIFIED, TodoStatus.CLOSED) and t.completed_at is None:
                t.completed_at = datetime.utcnow()
            elif new_st not in (TodoStatus.VERIFIED, TodoStatus.CLOSED):
                t.completed_at = None
        elif key == "priority" and val is not None:
            t.priority = _resolve_priority(val, t.priority)
        elif key == "item_type" and val is not None:
            t.item_type = _resolve_type(val, t.item_type)
        else:
            setattr(t, _PAYLOAD_KEY_MAP.get(key, key), val)
    new_assignee = t.assigned_to
    new_type = t.assigned_to_type or "user"
    if (new_assignee or "") != (prev_assignee or "") or new_type != prev_type:
        assignment_changed = True
        if new_assignee:
            t.assigned_by = user.username
            t.assigned_at = datetime.utcnow()
        else:
            # 取消指派 → 清掉 audit 欄位
            t.assigned_by = None
            t.assigned_at = None
    await db.flush()
    if assignment_changed and new_assignee:
        await _notify_assignment(db, t, user)
        await db.flush()
    await db.refresh(t)
    from app.services import entity_version_service as evs
    await evs.snapshot(
        db, entity_type="todo", entity=t,
        source=evs.CHANGE_SOURCE_HUMAN, status=evs.CONTENT_STATUS_PENDING,
        by=user.username,
    )
    return _enrich(t)


@router.post("/todos/{todo_id}/assign", response_model=TodoItemResponse, tags=["T · 待辦"])
async def assign_todo(
    todo_id: str,
    payload: TodoAssignRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """專責指派端點:單一動作改 assigned_to + 寫 audit + 推通知。
    不傳 assignee 或傳空字串 = 取消指派(API 對外仍叫 assignee)。"""
    t = await db.get(TodoItem, todo_id)
    _check_todo_scope(t, user)
    if payload.assignee_type not in ("user", "group"):
        raise HTTPException(400, "assignee_type 必須是 user 或 group")
    new_assignee = (payload.assignee or "").strip() or None
    prev = (t.assigned_to or "", t.assigned_to_type or "user")
    t.assigned_to = new_assignee
    t.assigned_to_type = payload.assignee_type if new_assignee else "user"
    if new_assignee:
        t.assigned_by = user.username
        t.assigned_at = datetime.utcnow()
    else:
        t.assigned_by = None
        t.assigned_at = None
    await db.flush()
    if (new_assignee or "", t.assigned_to_type) != prev and new_assignee:
        await _notify_assignment(db, t, user)
        await db.flush()
    await db.refresh(t)
    return _enrich(t)


@router.delete("/todos/{todo_id}", status_code=204, tags=["T · 待辦"])
async def delete_todo(
    todo_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    t = await db.get(TodoItem, todo_id)
    _check_todo_scope(t, user)
    await db.delete(t)
    await db.flush()
