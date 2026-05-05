"""Generic assignment endpoints (Phase 2).

Routes:
    POST   /api/assignments        — create / update assignment
    DELETE /api/assignments        — clear (params: entity_type, entity_id)
    GET    /api/assignments/me     — list things assigned to current user

Why one router instead of five (per-entity)?
    The five reviewable entities all mix in `Assignable` from
    app.auth.tenant. The schema is identical, so write the route once,
    dispatch to the correct model with a small lookup table.

Reuses:
    * /api/auth/users/assignable -- already loads org users
    * /api/settings/groups       -- already loads groups + members
    * notification_dispatch.notify -- pushes "assignment.received" to
      the assignee's inbox AND emails them when their preference says so

The lock semantics from RFC-Review-1 do NOT apply here -- assignment is
metadata, not content; an approved entity can still be reassigned.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.database import get_db
from app.models.group import Group
from app.models.user import User
from app.schemas.assignment import (
    AssignableEntityType,
    AssignmentResponse,
    AssignRequest,
)
from app.services.group_resolver import resolve_group_members
from app.services.notification_dispatch import notify

router = APIRouter()


# ── Entity dispatch table ────────────────────────────────────────────────

def _model_for(entity_type: AssignableEntityType):
    """Look up the SQLAlchemy model that holds the assignment columns for
    a given entity_type. Imported lazily to avoid circular imports."""
    from app.models.defect import Defect
    from app.models.requirement import Requirement
    from app.models.review import ReviewRecord
    from app.models.test_document import TestDocument
    from app.models.tree_node import LevelType, TreeNode

    return {
        AssignableEntityType.REVIEW: (ReviewRecord, None),
        AssignableEntityType.DEFECT: (Defect, None),
        AssignableEntityType.TESTCASE: (TreeNode, LevelType.TESTCASE),
        AssignableEntityType.REQUIREMENT: (Requirement, None),
        AssignableEntityType.DOCUMENT: (TestDocument, None),
    }[entity_type]


def _human_label(entity_type: AssignableEntityType, obj) -> str:
    """Render a short label for the notification title.

    Each entity has a different "name" attribute -- pick whichever is
    most useful to surface in the bell."""
    if entity_type == AssignableEntityType.REVIEW:
        return f"{obj.entity_type.value} {obj.entity_id[:8]}"
    if entity_type in (AssignableEntityType.TESTCASE, AssignableEntityType.REQUIREMENT):
        return getattr(obj, "name", obj.id)
    if entity_type == AssignableEntityType.DOCUMENT:
        return getattr(obj, "title", obj.id)
    if entity_type == AssignableEntityType.DEFECT:
        return f"{obj.code} {obj.title[:40]}" if obj.title else obj.code
    return obj.id


async def _load_for_assignment(
    db: AsyncSession,
    entity_type: AssignableEntityType,
    entity_id: str,
    user: User,
):
    """Resolve obj + scope-check it. 404 if not in user's org / wrong type."""
    Model, level_filter = _model_for(entity_type)
    obj = await db.get(Model, entity_id)
    if obj is None:
        raise HTTPException(status_code=404, detail=f"{entity_type.value} not found")
    # Tenant check
    obj_org = getattr(obj, "organization_id", None)
    if not user.is_superuser and obj_org and obj_org != user.organization_id:
        raise HTTPException(status_code=404, detail=f"{entity_type.value} not found")
    # TESTCASE-level filter for TreeNode
    if level_filter is not None and getattr(obj, "level_type", None) != level_filter:
        raise HTTPException(
            status_code=400,
            detail="only TESTCASE nodes are assignable; others are organisational containers",
        )
    return obj


# ── Endpoints ────────────────────────────────────────────────────────────

@router.post(
    "/assignments",
    response_model=AssignmentResponse,
    status_code=200,
    tags=["AC · 指派"],
)
async def assign(
    payload: AssignRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Set or replace the assignee on an entity. Notifies the assignee."""
    obj = await _load_for_assignment(db, payload.entity_type, payload.entity_id, user)

    obj.assigned_to = payload.assignee
    obj.assigned_to_type = payload.assignee_type.value
    obj.assigned_by = user.username
    obj.assigned_at = datetime.utcnow()
    await db.flush()
    await db.refresh(obj)

    # Push notification + email (when subscriber has email enabled).
    # Notify everyone affected: for user-type assignment, just the assignee;
    # for group-type, recursively expand to all members (incl. nested
    # subgroups) via the shared resolver — Tier D-2 finished what was
    # marked "v1.2 deferred" before, now matches TodoItem behaviour.
    label = _human_label(payload.entity_type, obj)
    org_id = getattr(obj, "organization_id", None)
    body_who = "您"
    targets: set[str] = set()
    type_value = payload.assignee_type.value if hasattr(payload.assignee_type, "value") else str(payload.assignee_type)
    if type_value == "group":
        g = await db.get(Group, payload.assignee)
        if g is not None:
            targets = await resolve_group_members(db, g.id)
            body_who = f"群組「{g.name}」"
    else:
        targets = {payload.assignee}
    targets.discard(user.username)    # 不打擾自己
    for recipient in targets:
        await notify(
            db=db,
            event_key="assignment.received",
            recipient=recipient,
            title=f"您被指派一筆 {payload.entity_type.value}",
            body=f"「{label}」由 {user.username} 指派給{body_who}。",
            level="info",
            link=None,
            related_entity_type=payload.entity_type.value,
            related_entity_id=payload.entity_id,
            organization_id=org_id,
        )

    return AssignmentResponse(
        entity_type=payload.entity_type,
        entity_id=payload.entity_id,
        assigned_to=obj.assigned_to,
        assigned_to_type=obj.assigned_to_type,
        assigned_by=obj.assigned_by,
        assigned_at=obj.assigned_at,
    )


@router.delete("/assignments", status_code=204, tags=["AC · 指派"])
async def unassign(
    entity_type: AssignableEntityType = Query(...),
    entity_id: str = Query(..., min_length=1),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Clear the assignee on an entity. No notification (silent)."""
    obj = await _load_for_assignment(db, entity_type, entity_id, user)
    obj.assigned_to = None
    obj.assigned_to_type = "user"  # reset to default
    obj.assigned_by = None
    obj.assigned_at = None
    await db.flush()


_ME_ENTITY_TYPES = [t.value for t in AssignableEntityType] + ["todo"]


@router.get(
    "/assignments/me",
    tags=["AC · 指派"],
)
async def list_my_assignments(
    entity_type: Optional[str] = Query(
        None,
        description="篩選單一 entity_type;接受 review / defect / testcase / requirement / document / todo",
    ),
    project_id: Optional[str] = Query(None),
    overdue: bool = Query(False, description="僅列 due_date < 今日"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """D-4 inbox 用 — 列出指派給我的 entity(可選 entity_type / project_id /
    overdue 過濾)。回傳已 enrich 過的 dict(含 title / due_date / 路由提示),
    比 AssignmentResponse 多了 UI 需要的欄位。

    `entity_type` 接受 raw string(讓 'todo' 也通過,而不是只接受 generic
    AssignableEntityType enum;TodoItem 雖在另一條 router 但 D-1 已對齊欄位
    名,所以一起在這個 endpoint 服務 inbox)。
    """
    from app.models.todo_item import TodoItem
    today = date.today().isoformat()

    if entity_type is not None and entity_type not in _ME_ENTITY_TYPES:
        raise HTTPException(
            400,
            f"未支援的 entity_type:{entity_type}(支援:{', '.join(_ME_ENTITY_TYPES)})",
        )

    # 把 raw string 收攏:None / 'todo' / 五種 generic 之一 → (generic enum list, include_todo)
    if entity_type is None:
        types = list(AssignableEntityType)
        include_todo = True
    elif entity_type == "todo":
        types = []
        include_todo = True
    else:
        types = [AssignableEntityType(entity_type)]
        include_todo = False

    out: list[dict] = []

    def _pick_label(et, r) -> str:
        return _human_label(et, r)

    def _row_to_dict(et, r) -> dict:
        return {
            "entity_type": et.value,
            "entity_id": r.id if et != AssignableEntityType.REVIEW else r.entity_id,
            "label": _pick_label(et, r),
            "project_id": getattr(r, "project_id", None),
            "due_date": getattr(r, "due_date", None),
            "status": getattr(r.status, "value", str(r.status)) if getattr(r, "status", None) is not None else None,
            "assigned_to": r.assigned_to,
            "assigned_to_type": r.assigned_to_type,
            "assigned_by": r.assigned_by,
            "assigned_at": r.assigned_at.isoformat() if r.assigned_at else None,
        }

    for et in types:
        Model, level_filter = _model_for(et)
        stmt = select(Model).where(Model.assigned_to == user.username)
        stmt = stmt.where(Model.assigned_to_type == "user")    # 群組指派由 D-2 fan-out 寫進 Notification 表
        if not user.is_superuser:
            stmt = stmt.where(Model.organization_id == user.organization_id)
        if level_filter is not None:
            stmt = stmt.where(Model.level_type == level_filter)
        if project_id and hasattr(Model, "project_id"):
            stmt = stmt.where(Model.project_id == project_id)
        rows = (await db.execute(stmt)).scalars().all()
        for r in rows:
            d = _row_to_dict(et, r)
            if overdue:
                if not d["due_date"] or d["due_date"] >= today:
                    continue
            out.append(d)

    if include_todo:
        tstmt = select(TodoItem).where(TodoItem.assigned_to == user.username).where(TodoItem.assigned_to_type == "user")
        if not user.is_superuser:
            tstmt = tstmt.where(TodoItem.organization_id == user.organization_id)
        if project_id:
            tstmt = tstmt.where(TodoItem.project_id == project_id)
        todos = (await db.execute(tstmt)).scalars().all()
        for t in todos:
            d = {
                "entity_type": "todo",
                "entity_id": t.id,
                "label": t.title,
                "project_id": t.project_id,
                "due_date": t.due_date,
                "status": t.status.value if hasattr(t.status, "value") else str(t.status),
                "assigned_to": t.assigned_to,
                "assigned_to_type": t.assigned_to_type,
                "assigned_by": t.assigned_by,
                "assigned_at": t.assigned_at.isoformat() if t.assigned_at else None,
            }
            if overdue and (not d["due_date"] or d["due_date"] >= today):
                continue
            out.append(d)

    # 再加上「群組裡的我」— 透過 fan-out 寫進 Notification 表的指派
    # 不在這個 endpoint 拉(只看直接指派);要看群組指派去 /api/notifications

    out.sort(key=lambda x: (x.get("due_date") or "9999", x.get("entity_type")))
    return out


# ─── D-3:bulk reassign + stale ─────────────────────────────────
@router.patch("/assignments/bulk", tags=["AC · 指派"])
async def bulk_reassign(
    payload: dict,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """一次 reassign 多筆同 entity_type 的 entity。
    body:`{"entity_type":"defect","entity_ids":[...],"assignee":"alice","assignee_type":"user"}`
    `assignee=null` 等同取消指派(無通知)。limit 200/call。
    回:`{updated: N, skipped: [{id, reason}]}`。
    通知策略:單 user 收件人會合併成一封「您被指派 N 筆 {entity_type}」(避免轟炸)。
    """
    raw_type = (payload or {}).get("entity_type")
    ids = (payload or {}).get("entity_ids") or []
    assignee = (payload or {}).get("assignee")
    assignee_type = (payload or {}).get("assignee_type") or "user"
    if not raw_type:
        raise HTTPException(400, "缺少 entity_type")
    try:
        et = AssignableEntityType(raw_type)
    except ValueError:
        raise HTTPException(400, f"未支援的 entity_type:{raw_type}")
    if not isinstance(ids, list) or not ids:
        raise HTTPException(400, "缺少 entity_ids(非空陣列)")
    if len(ids) > 200:
        raise HTTPException(400, "一次最多 200 筆")
    if assignee_type not in ("user", "group"):
        raise HTTPException(400, "assignee_type 必須是 user 或 group")

    Model, level_filter = _model_for(et)
    updated = 0
    skipped: list[dict] = []
    affected: list = []
    for eid in ids:
        try:
            obj = await _load_for_assignment(db, et, eid, user)
        except HTTPException as e:
            skipped.append({"id": eid, "reason": str(e.detail)}); continue
        obj.assigned_to = assignee or None
        obj.assigned_to_type = assignee_type if assignee else "user"
        if assignee:
            obj.assigned_by = user.username
            obj.assigned_at = datetime.utcnow()
        else:
            obj.assigned_by = None
            obj.assigned_at = None
        affected.append(obj)
        updated += 1
    await db.flush()

    # 合併通知
    if assignee and affected:
        org_id = getattr(affected[0], "organization_id", None)
        if assignee_type == "group":
            g = await db.get(Group, assignee)
            if g is not None:
                recipients = await resolve_group_members(db, g.id)
                recipients.discard(user.username)
                body_label = f"群組「{g.name}」"
            else:
                recipients = set()
                body_label = "(group 已不存在)"
        else:
            recipients = {assignee} - {user.username}
            body_label = "您"
        title = f"您被指派 {updated} 筆 {et.value}"
        body = f"{user.username} 一次將 {updated} 筆 {et.value} 指派給{body_label}。"
        for r in recipients:
            await notify(
                db=db,
                event_key="assignment.received",
                recipient=r,
                title=title,
                body=body,
                level="info",
                link=None,
                related_entity_type=et.value,
                related_entity_id=None,
                organization_id=org_id,
            )

    return {"updated": updated, "skipped": skipped}


@router.get("/assignments/stale", tags=["AC · 指派"])
async def list_stale_assignments(
    entity_type: Optional[AssignableEntityType] = Query(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """列出「指派對象已不存在」的 entity:
    * assignee_type='user' 但該 user 已不在這個 org(OrgMembership active)
    * assignee_type='group' 但該 group 已被刪除
    讓 admin 一覽 + 一鍵 reassign(走 /assignments/bulk)。
    """
    from app.models.org_membership import OrgMembership
    from app.models.todo_item import TodoItem

    types = [entity_type] if entity_type else list(AssignableEntityType)
    target_org = None if user.is_superuser else user.organization_id

    # 一次性撈出該 org 所有 active OrgMembership 的 username,再撈所有 group ids
    active_users_stmt = select(OrgMembership.username).where(OrgMembership.status == "active")
    if target_org:
        active_users_stmt = active_users_stmt.where(OrgMembership.organization_id == target_org)
    active_users = set((await db.execute(active_users_stmt)).scalars().all())

    all_groups_stmt = select(Group.id)
    if target_org:
        all_groups_stmt = all_groups_stmt.where((Group.organization_id == target_org) | (Group.organization_id.is_(None)))
    valid_groups = set((await db.execute(all_groups_stmt)).scalars().all())

    out: list[dict] = []

    def _check(et_value: str, r, label: str) -> Optional[dict]:
        if r.assigned_to is None:
            return None
        if r.assigned_to_type == "user" and r.assigned_to not in active_users:
            reason = "user 已不在 org / 不再 active"
        elif r.assigned_to_type == "group" and r.assigned_to not in valid_groups:
            reason = "group 已刪除"
        else:
            return None
        return {
            "entity_type": et_value,
            "entity_id": r.id if et_value != "review" else r.entity_id,
            "label": label,
            "assigned_to": r.assigned_to,
            "assigned_to_type": r.assigned_to_type,
            "reason": reason,
        }

    for et in types:
        Model, level_filter = _model_for(et)
        stmt = select(Model).where(Model.assigned_to.is_not(None))
        if target_org:
            stmt = stmt.where(Model.organization_id == target_org)
        if level_filter is not None:
            stmt = stmt.where(Model.level_type == level_filter)
        rows = (await db.execute(stmt)).scalars().all()
        for r in rows:
            d = _check(et.value, r, _human_label(et, r))
            if d:
                out.append(d)

    if entity_type is None:    # TodoItem 也檢查
        tstmt = select(TodoItem).where(TodoItem.assigned_to.is_not(None))
        if target_org:
            tstmt = tstmt.where(TodoItem.organization_id == target_org)
        for t in (await db.execute(tstmt)).scalars().all():
            d = _check("todo", t, t.title)
            if d:
                out.append(d)

    return out
