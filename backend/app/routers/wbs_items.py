"""WBS REST endpoints。"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
import sqlalchemy as sa
from sqlalchemy import asc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.auth.project_membership import ensure_project_member
from app.auth.scope import (
    ensure_project_in_scope,
    ensure_project_writable,
    scope_by_project,
)
from app.common import Pagination
from app.database import get_db
from app.models.user import User
from app.models.wbs_item import WbsItem, WbsItemType, WbsStatus
from app.models.wbs_link import ALLOWED_WBS_TARGET_TYPES, WbsLink
from app.schemas.wbs_item import WbsItemCreate, WbsItemResponse, WbsItemUpdate

router = APIRouter()


async def _next_code(db: AsyncSession, project_id: str) -> str:
    result = await db.execute(
        select(func.count(WbsItem.id)).where(WbsItem.project_id == project_id)
    )
    n = (result.scalar_one_or_none() or 0) + 1
    return f"WBS-{n:03d}"


_LEGACY_WBS_STATUS = {
    "NotStarted": "New", "Completed": "Verified",
    "Blocked": "ReworkRequired", "Cancelled": "Closed",
}


def _resolve_status(val, default):
    if val is None:
        return default
    val = _LEGACY_WBS_STATUS.get(val, val)
    try:
        return WbsStatus(val)
    except ValueError:
        return default


@router.get(
    "/wbs",
    response_model=list[WbsItemResponse],
    tags=["R · WBS"],
    dependencies=[Depends(ensure_project_member)],
)
async def list_wbs(
    project_id: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    page: Pagination = Depends(Pagination.from_query),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """整批回傳 — 前端依 parent_id 自行組樹。"""
    stmt = select(WbsItem).order_by(asc(WbsItem.sort_order), asc(WbsItem.code))
    if project_id:
        stmt = stmt.where(WbsItem.project_id == project_id)
    if status:
        norm = _LEGACY_WBS_STATUS.get(status, status)
        stmt = stmt.where(WbsItem.status == WbsStatus(norm))
    stmt = scope_by_project(stmt, WbsItem, user)
    stmt = page.apply(stmt)
    rows = (await db.execute(stmt)).scalars().all()
    return list(rows)


@router.get(
    "/wbs/tree",
    tags=["R · WBS"],
    dependencies=[Depends(ensure_project_member)],
)
async def wbs_tree(
    project_id: str = Query(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await ensure_project_in_scope(db, project_id, user, not_found_detail="Project not found")
    """以巢狀 JSON 回傳整棵 WBS 樹（root_items + children 遞迴展開）。"""
    rows = (await db.execute(
        select(WbsItem)
        .where(WbsItem.project_id == project_id)
        .order_by(asc(WbsItem.sort_order), asc(WbsItem.code))
    )).scalars().all()

    by_id: dict[str, dict] = {}
    for r in rows:
        by_id[r.id] = {
            "id": r.id,
            "project_id": r.project_id,
            "parent_id": r.parent_id,
            "code": r.code,
            "name": r.name,
            "description": r.description,
            "item_type": r.item_type.value if hasattr(r.item_type, "value") else str(r.item_type),
            "status": r.status.value if hasattr(r.status, "value") else str(r.status),
            "progress": r.progress,
            "assignee": r.assignee,
            "start_date": r.start_date,
            "end_date": r.end_date,
            "effort_hours": r.effort_hours,
            "sort_order": r.sort_order,
            "children": [],
            "link_counts": {"todo": 0, "testcase": 0, "defect": 0, "execution_report": 0},
        }

    # 同步把 wbs_links 的計數也放進每個 Task 節點(讓 UI 一眼看到 4 種連結各有幾筆)
    if rows:
        ids = [r.id for r in rows]
        link_rows = (await db.execute(
            select(WbsLink.wbs_item_id, WbsLink.target_type, sa.func.count(WbsLink.id))
            .where(WbsLink.wbs_item_id.in_(ids))
            .group_by(WbsLink.wbs_item_id, WbsLink.target_type)
        )).all()
        for wbs_id, ttype, cnt in link_rows:
            counts = by_id.get(wbs_id, {}).get("link_counts")
            if counts is not None and ttype in counts:
                counts[ttype] = int(cnt)

    roots: list[dict] = []
    for r in rows:
        node = by_id[r.id]
        if r.parent_id and r.parent_id in by_id:
            by_id[r.parent_id]["children"].append(node)
        else:
            roots.append(node)
    return roots


@router.post("/wbs", response_model=WbsItemResponse, status_code=201, tags=["R · WBS"])
async def create_wbs(
    payload: WbsItemCreate,
    from_ai: bool = False,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await ensure_project_writable(db, payload.project_id, user)
    code = payload.code or await _next_code(db, payload.project_id)
    if payload.parent_id:
        parent = await db.get(WbsItem, payload.parent_id)
        if not parent or parent.project_id != payload.project_id:
            raise HTTPException(400, "parent_id 不存在或不屬於同專案")
    # 決定 item_type:payload 給的 → 若沒給,看 parent 來推斷
    #   parent=None → Feature(根層)
    #   parent=Feature → WorkPackage
    #   parent=WorkPackage → Task
    #   parent=Task → 仍 Task(Task 下不允許再有層,但容許多層 Task 巢狀以彈性)
    requested_type = (payload.item_type or "").strip()
    if requested_type:
        try:
            resolved_type = WbsItemType(requested_type)
        except ValueError:
            raise HTTPException(400, f"item_type 必須是 Feature / WorkPackage / Task")
    else:
        if not payload.parent_id:
            resolved_type = WbsItemType.FEATURE
        else:
            parent_obj = await db.get(WbsItem, payload.parent_id)
            ptype = (parent_obj.item_type if parent_obj else WbsItemType.TASK)
            resolved_type = {
                WbsItemType.FEATURE: WbsItemType.WORK_PACKAGE,
                WbsItemType.WORK_PACKAGE: WbsItemType.TASK,
                WbsItemType.TASK: WbsItemType.TASK,
            }.get(ptype, WbsItemType.TASK)
    item = WbsItem(
        project_id=payload.project_id,
        parent_id=payload.parent_id,
        code=code,
        name=payload.name,
        description=payload.description,
        item_type=resolved_type,
        status=_resolve_status(payload.status, WbsStatus.NOT_STARTED),
        progress=max(0, min(100, payload.progress or 0)),
        assignee=payload.assignee,
        start_date=payload.start_date,
        end_date=payload.end_date,
        effort_hours=payload.effort_hours,
        sort_order=payload.sort_order or 0,
    )
    db.add(item)
    await db.flush()
    await db.refresh(item)
    from app.services import entity_version_service as evs
    status_v = evs.CONTENT_STATUS_AI_DRAFT if from_ai else evs.CONTENT_STATUS_PENDING
    source_v = evs.CHANGE_SOURCE_AI if from_ai else evs.CHANGE_SOURCE_HUMAN
    await evs.snapshot(
        db, entity_type="wbs_item", entity=item,
        source=source_v, status=status_v, by=user.username,
    )
    return item


@router.get("/wbs/{item_id}", response_model=WbsItemResponse, tags=["R · WBS"])
async def get_wbs(
    item_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    item = await db.get(WbsItem, item_id)
    await ensure_project_in_scope(
        db, item.project_id if item else None, user, not_found_detail="WBS item not found"
    )
    return item


@router.put("/wbs/{item_id}", response_model=WbsItemResponse, tags=["R · WBS"])
async def update_wbs(
    item_id: str,
    payload: WbsItemUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    item = await db.get(WbsItem, item_id)
    await ensure_project_in_scope(
        db, item.project_id if item else None, user, not_found_detail="WBS item not found"
    )
    data = payload.model_dump(exclude_unset=True)
    if "parent_id" in data and data["parent_id"]:
        if data["parent_id"] == item.id:
            raise HTTPException(400, "parent_id 不能等於自己")
        parent = await db.get(WbsItem, data["parent_id"])
        if not parent or parent.project_id != item.project_id:
            raise HTTPException(400, "parent_id 不存在或不屬於同專案")
    for key, val in data.items():
        if key == "status" and val is not None:
            item.status = _resolve_status(val, item.status)
        elif key == "progress" and val is not None:
            item.progress = max(0, min(100, int(val)))
        elif key == "item_type" and val is not None:
            try:
                item.item_type = WbsItemType(val)
            except ValueError:
                raise HTTPException(400, "item_type 必須是 Feature / WorkPackage / Task")
        else:
            setattr(item, key, val)
    await db.flush()
    await db.refresh(item)
    from app.services import entity_version_service as evs
    await evs.snapshot(
        db, entity_type="wbs_item", entity=item,
        source=evs.CHANGE_SOURCE_HUMAN, status=evs.CONTENT_STATUS_PENDING,
        by=user.username,
    )
    return item


@router.delete("/wbs/{item_id}", status_code=204, tags=["R · WBS"])
async def delete_wbs(
    item_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """刪除 WBS 項目(含其所有子項,由 DB cascade 自動刪除)。"""
    item = await db.get(WbsItem, item_id)
    await ensure_project_in_scope(
        db, item.project_id if item else None, user, not_found_detail="WBS item not found"
    )
    await db.delete(item)
    await db.flush()


# ─── WbsLink:Task 葉節點連到 todo / testcase / defect / execution_report ──
async def _enrich_wbs_link(db: AsyncSession, link: WbsLink) -> dict:
    """補上 target 物件的可讀欄位(label / status 等),前端不用再多打一次 API。"""
    base = {
        "id": link.id,
        "wbs_item_id": link.wbs_item_id,
        "target_type": link.target_type,
        "target_id": link.target_id,
        "created_by": link.created_by,
        "created_at": link.created_at.isoformat() if link.created_at else None,
        "label": None,
        "status": None,
        "code": None,
    }
    try:
        if link.target_type == "todo":
            from app.models.todo_item import TodoItem
            o = await db.get(TodoItem, link.target_id)
            if o:
                base.update({"label": o.title, "status": o.status.value if hasattr(o.status, "value") else o.status})
        elif link.target_type == "testcase":
            from app.models.tree_node import TreeNode
            o = await db.get(TreeNode, link.target_id)
            if o:
                base.update({"label": o.name, "status": o.content_status})
        elif link.target_type == "defect":
            from app.models.defect import Defect
            o = await db.get(Defect, link.target_id)
            if o:
                base.update({
                    "label": o.title,
                    "code": o.code,
                    "status": o.status.value if hasattr(o.status, "value") else o.status,
                })
        elif link.target_type == "execution_report":
            from app.models.execution_report import ExecutionReport
            o = await db.get(ExecutionReport, link.target_id)
            if o:
                base.update({
                    "label": getattr(o, "name", None) or getattr(o, "trigger_user", None) or link.target_id[:8],
                    "status": o.status.value if hasattr(o.status, "value") else getattr(o, "status", None),
                })
    except Exception:
        pass
    return base


@router.get("/wbs/{item_id}/links", tags=["R · WBS"])
async def list_wbs_links(
    item_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """列出此 WBS Task 連到的所有外部實體(任務 / 測試案例 / 缺陷 / 執行紀錄)。"""
    item = await db.get(WbsItem, item_id)
    await ensure_project_in_scope(
        db, item.project_id if item else None, user, not_found_detail="WBS item not found"
    )
    links = (await db.execute(
        select(WbsLink).where(WbsLink.wbs_item_id == item_id).order_by(WbsLink.created_at)
    )).scalars().all()
    return [await _enrich_wbs_link(db, l) for l in links]


@router.post("/wbs/{item_id}/links", status_code=201, tags=["R · WBS"])
async def create_wbs_link(
    item_id: str,
    payload: dict,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """連結一個外部實體到 WBS Task。
    body: ``{"target_type": "todo|testcase|defect|execution_report", "target_id": "..."}``"""
    item = await db.get(WbsItem, item_id)
    await ensure_project_in_scope(
        db, item.project_id if item else None, user, not_found_detail="WBS item not found"
    )
    target_type = (payload or {}).get("target_type", "").strip()
    target_id = (payload or {}).get("target_id", "").strip()
    if target_type not in ALLOWED_WBS_TARGET_TYPES:
        raise HTTPException(400, f"target_type 必須是 {sorted(ALLOWED_WBS_TARGET_TYPES)}")
    if not target_id:
        raise HTTPException(400, "缺少 target_id")
    # dedupe(同 wbs_item + target 組合只一筆)
    existing = (await db.execute(
        select(WbsLink)
        .where(WbsLink.wbs_item_id == item_id)
        .where(WbsLink.target_type == target_type)
        .where(WbsLink.target_id == target_id)
    )).scalar_one_or_none()
    if existing:
        return await _enrich_wbs_link(db, existing)
    link = WbsLink(
        wbs_item_id=item_id,
        target_type=target_type,
        target_id=target_id,
        created_by=user.username,
        organization_id=item.organization_id,
    )
    db.add(link)
    await db.flush()
    await db.refresh(link)
    return await _enrich_wbs_link(db, link)


@router.delete("/wbs/{item_id}/links/{link_id}", status_code=204, tags=["R · WBS"])
async def delete_wbs_link(
    item_id: str,
    link_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    item = await db.get(WbsItem, item_id)
    await ensure_project_in_scope(
        db, item.project_id if item else None, user, not_found_detail="WBS item not found"
    )
    link = await db.get(WbsLink, link_id)
    if not link or link.wbs_item_id != item_id:
        raise HTTPException(404, "Link not found")
    await db.delete(link)
    await db.flush()
