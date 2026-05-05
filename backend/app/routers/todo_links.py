"""TodoLink REST endpoints — Backlog 跨實體連結 CRUD + 反向查詢。

功能:
- 任一 TodoItem 連到任一目標實體(N:M)
- 反向查詢:某實體被哪些 Todo 連到(給缺陷卡片 / 需求清單徽章用)
- 批次反查:給 RTM 矩陣 / 看板畫面用,1 round-trip 拿全部徽章資料
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.auth.project_membership import ensure_project_member
from app.database import get_db
from app.models.defect import Defect
from app.models.project import Project
from app.models.requirement import Requirement
from app.models.test_document import TestDocument
from app.models.test_milestone import TestMilestone
from app.models.test_plan import TestPlan
from app.models.test_round import TestRound
from app.models.test_version import TestVersion
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
    "test_version": (TestVersion, "version_label", "platform"),  # G-1:顯示為「[WEB] v1.5-rc1」
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
    # Enum 欄位(例如 TestVersion.platform)→ 取 .value 否則前端顯示會是 "VersionPlatform.WEB"
    if hasattr(title, "value"):
        title = title.value
    if hasattr(code, "value"):
        code = code.value
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
    target_title, _ = await _validate_target(db, payload.target_type, payload.target_id)

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
    # G-5:通知 target 的 user-type assignee(若存在 + 非 actor 自己)。
    # 群組指派不 fan-out(避免噪音);testcase / test_version 等 entity 沒
    # assignee 欄位 → 自動跳過。
    await _notify_link_target(db, todo, link, target_title, user)
    return await _enrich_link(db, link)


async def _notify_link_target(
    db: AsyncSession,
    todo: TodoItem,
    link: TodoLink,
    target_title: Optional[str],
    actor: User,
) -> None:
    """G-5:把 todo 連到某個 entity 時,通知該 entity 的 user 類型 assignee。
    若 entity 沒 assigned_to / assigned_to 是 group / 對象就是 actor 自己 → 跳過。"""
    spec = _TARGET_REGISTRY.get(link.target_type)
    if not spec:
        return    # testcase / 其他特殊 type 不通知
    Model, _, _ = spec
    obj = await db.get(Model, link.target_id)
    if obj is None:
        return
    recipient = getattr(obj, "assigned_to", None)
    rcp_type = getattr(obj, "assigned_to_type", None) or "user"
    if not recipient or rcp_type != "user" or recipient == actor.username:
        return
    try:
        from app.services.notification_dispatch import notify
        kind_label = {
            "relates_to": "相關", "verifies": "驗證",
            "blocks": "阻擋", "duplicates": "重複",
        }.get(link.link_kind or "relates_to", link.link_kind or "relates_to")
        await notify(
            db=db,
            event_key="assignment.received",
            recipient=recipient,
            title=f"待辦連結到您負責的 {link.target_type}",
            body=f"{actor.username} 在待辦「{todo.title}」加入指向 {link.target_type}「{target_title or link.target_id}」 的連結 ({kind_label})。",
            level="info",
            link=None,
            related_entity_type=link.target_type,
            related_entity_id=link.target_id,
            organization_id=todo.organization_id,
        )
    except Exception:    # noqa: BLE001
        import logging
        logging.getLogger(__name__).exception(
            "_notify_link_target failed for link=%s target=%s",
            link.id, link.target_id,
        )


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
            assignee=t.assigned_to,
            link_kind=l.link_kind,
        )
        for (l, t) in rows
    ]


# ── 5) 批次反向查詢:整個專案某 type 一次拿完 ────────────────────────
# 給看板 / 需求清單 / RTM 矩陣畫徽章用,1 round-trip 拿全部 link 資訊。
@router.get(
    "/links/by-target/batch",
    tags=["T · 待辦"],
    dependencies=[Depends(ensure_project_member)],
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
            "assignee": t.assigned_to,
            "link_kind": l.link_kind,
        })
    return grouped


# ── G-4:bulk 建立 todo + 自動連結到一批 target ────────────────
@router.post("/todos/bulk-from-targets", tags=["T · 待辦"])
async def bulk_create_todos_from_targets(
    payload: dict,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """G-4 — 從一批 target entity 一次建立 N 筆「追蹤待辦」並自動連結。
    主要 use case:Sprint planning / triage 時從 defect 或 testcase list
    勾選 N 筆 → 一鍵幫每個建一筆 todo。

    body:
    ```
    {
      "target_type": "defect",                # 必填,白名單同 ALLOWED_TARGET_TYPES
      "target_ids": ["...", "..."],           # 必填,1-200
      "project_id": "...",                    # 可選(不傳 = 從第一筆 target 推導)
      "title_template": "追蹤 {code} {title}", # 可空,預設 "追蹤 {code} {title}"
      "link_kind": "relates_to",              # 可選,預設 relates_to
      "due_date": "2026-05-15",               # 可選
      "priority": "P2",                       # 可選
      "sprint_label": null,                   # 可選
      "assignee": null,                       # 可選,預設不指派
    }
    ```
    回:`{created: [{todo_id, target_id, link_id}, ...], skipped: [{target_id, reason}]}`。
    通知策略(G-5 整合):合併成一封「{actor} 從 N 筆 {target_type} 批次建立追蹤待辦」
    給每個 unique recipient(去重 actor),避免轟炸。
    """
    target_type = (payload or {}).get("target_type")
    target_ids = (payload or {}).get("target_ids") or []
    if not target_type:
        raise HTTPException(400, "缺少 target_type")
    if target_type not in ALLOWED_TARGET_TYPES:
        raise HTTPException(400, f"target_type 不支援:{target_type}")
    if not isinstance(target_ids, list) or not target_ids:
        raise HTTPException(400, "缺少 target_ids(非空陣列)")
    if len(target_ids) > 200:
        raise HTTPException(400, "一次最多 200 筆")

    title_template = (payload or {}).get("title_template") or "追蹤 {code} {title}"
    link_kind = (payload or {}).get("link_kind") or "relates_to"
    due_date = (payload or {}).get("due_date")
    priority_str = (payload or {}).get("priority") or "P2"
    sprint_label = (payload or {}).get("sprint_label")
    assignee = (payload or {}).get("assignee")
    project_id_in = (payload or {}).get("project_id")

    from app.models.todo_item import TodoItem, TodoItemType, TodoPriority, TodoStatus
    try:
        prio_enum = TodoPriority(priority_str)
    except ValueError:
        prio_enum = TodoPriority.P2

    created: list[dict] = []
    skipped: list[dict] = []
    notify_recipients: dict[str, str] = {}    # username → label(去重)

    spec = _TARGET_REGISTRY.get(target_type)
    Model = spec[0] if spec else None    # testcase 走 TreeNode 特例

    for tid in target_ids:
        # 1) 驗證 target + 拿到 title/code
        try:
            tt, tc = await _validate_target(db, target_type, tid)
        except HTTPException as e:
            skipped.append({"target_id": tid, "reason": str(e.detail)}); continue

        # 2) 取 entity 用於推導 project_id / assignee 通知對象
        if target_type == "testcase":
            from app.models.tree_node import TreeNode
            target_obj = await db.get(TreeNode, tid)
        else:
            target_obj = await db.get(Model, tid) if Model else None

        # 3) project_id:優先用 payload,其次推導
        pid = project_id_in or getattr(target_obj, "project_id", None) or user.organization_id

        # 4) 套 title_template
        try:
            todo_title = title_template.format(
                code=tc or "",
                title=tt or "",
                name=tt or "",
            ).strip()
            if not todo_title:
                todo_title = f"追蹤 {target_type} {tid[:8]}"
        except (KeyError, IndexError):
            todo_title = f"追蹤 {tt or tid[:8]}"

        # 5) 建 todo
        now = datetime.utcnow() if assignee else None
        todo = TodoItem(
            project_id=pid,
            organization_id=user.organization_id,
            title=todo_title,
            status=TodoStatus.TODO,
            priority=prio_enum,
            due_date=due_date,
            sprint_label=sprint_label,
            assigned_to=assignee,
            assigned_to_type="user" if assignee else "user",
            assigned_by=user.username if assignee else None,
            assigned_at=now,
            item_type=TodoItemType.TASK,
        )
        db.add(todo)
        await db.flush()

        # 6) 建 link
        link = TodoLink(
            organization_id=user.organization_id or todo.organization_id,
            todo_id=todo.id,
            target_type=target_type,
            target_id=tid,
            link_kind=link_kind,
            created_by=user.username,
        )
        db.add(link)
        await db.flush()

        created.append({"todo_id": todo.id, "target_id": tid, "link_id": link.id})

        # 7) 收集通知對象(target 的 user-type assignee,排除 actor)
        rcp = getattr(target_obj, "assigned_to", None) if target_obj else None
        rcp_type = (getattr(target_obj, "assigned_to_type", None) or "user") if target_obj else "user"
        if rcp and rcp_type == "user" and rcp != user.username:
            notify_recipients.setdefault(rcp, target_type)

    # 8) 合併通知:每個 unique recipient 一封「您負責的 N 筆 {target_type} 被連到追蹤待辦」
    if created and notify_recipients:
        try:
            from app.services.notification_dispatch import notify
            for recipient, ttype in notify_recipients.items():
                await notify(
                    db=db,
                    event_key="assignment.received",
                    recipient=recipient,
                    title=f"您負責的 {ttype} 被連到追蹤待辦",
                    body=f"{user.username} 從 {len(created)} 筆 {ttype} 批次建立追蹤待辦,其中包含您負責的項目。",
                    level="info",
                    link=None,
                    related_entity_type=ttype,
                    related_entity_id=None,
                    organization_id=user.organization_id,
                )
        except Exception:    # noqa: BLE001
            import logging
            logging.getLogger(__name__).exception("bulk-from-targets notify failed")

    return {"created": created, "skipped": skipped}
