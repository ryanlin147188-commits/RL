"""WBS REST endpoints。"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
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
from app.models.wbs_item import WbsItem, WbsStatus
from app.schemas.wbs_item import WbsItemCreate, WbsItemResponse, WbsItemUpdate

router = APIRouter()


async def _next_code(db: AsyncSession, project_id: str) -> str:
    result = await db.execute(
        select(func.count(WbsItem.id)).where(WbsItem.project_id == project_id)
    )
    n = (result.scalar_one_or_none() or 0) + 1
    return f"WBS-{n:03d}"


def _resolve_status(val, default):
    if val is None:
        return default
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
        stmt = stmt.where(WbsItem.status == WbsStatus(status))
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
            "status": r.status.value if hasattr(r.status, "value") else str(r.status),
            "progress": r.progress,
            "assignee": r.assignee,
            "start_date": r.start_date,
            "end_date": r.end_date,
            "effort_hours": r.effort_hours,
            "sort_order": r.sort_order,
            "children": [],
        }

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
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await ensure_project_writable(db, payload.project_id, user)
    code = payload.code or await _next_code(db, payload.project_id)
    if payload.parent_id:
        parent = await db.get(WbsItem, payload.parent_id)
        if not parent or parent.project_id != payload.project_id:
            raise HTTPException(400, "parent_id 不存在或不屬於同專案")
    item = WbsItem(
        project_id=payload.project_id,
        parent_id=payload.parent_id,
        code=code,
        name=payload.name,
        description=payload.description,
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
        else:
            setattr(item, key, val)
    await db.flush()
    await db.refresh(item)
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
