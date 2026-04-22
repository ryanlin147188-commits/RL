"""排程（Schedule）REST endpoints。

- GET    /schedules                      列出所有排程（可選 project_id 過濾）
- POST   /schedules                      建立排程
- GET    /schedules/{id}                 取得單一排程
- PUT    /schedules/{id}                 更新排程（任一欄位）
- DELETE /schedules/{id}                 刪除排程
- POST   /schedules/{id}/trigger-now     立即觸發一次（不影響 next_run_at）
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.schedule import RepeatType, Schedule
from app.models.tree_node import TreeNode
from app.schemas.schedule import ScheduleCreate, ScheduleResponse, ScheduleUpdate
from app.services.schedule_service import _trigger_schedule, compute_next_run

router = APIRouter()


def _get_node_ids(schedule: Schedule) -> list[str]:
    """從 schedule 還原多選節點 id 清單；node_ids_json 優先，沒有才退化為 [node_id]。"""
    raw = getattr(schedule, "node_ids_json", None)
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list) and parsed:
                # 去重但保留順序
                seen: set[str] = set()
                out: list[str] = []
                for nid in parsed:
                    if isinstance(nid, str) and nid and nid not in seen:
                        seen.add(nid)
                        out.append(nid)
                if out:
                    return out
        except Exception:
            pass
    return [schedule.node_id] if schedule.node_id else []


def _to_response(
    schedule: Schedule,
    node_title: Optional[str],
    node_titles: Optional[list[str]] = None,
    node_ids: Optional[list[str]] = None,
) -> ScheduleResponse:
    nids = node_ids if node_ids is not None else _get_node_ids(schedule)
    return ScheduleResponse(
        id=schedule.id,
        name=schedule.name,
        node_id=schedule.node_id,
        node_ids=nids,
        project_id=schedule.project_id,
        node_title=node_title,
        node_titles=node_titles or [],
        repeat_type=schedule.repeat_type.value
        if isinstance(schedule.repeat_type, RepeatType)
        else schedule.repeat_type,
        repeat_config=schedule.repeat_config,
        next_run_at=schedule.next_run_at,
        last_run_at=schedule.last_run_at,
        last_report_id=schedule.last_report_id,
        active=schedule.active,
        execution_mode=getattr(schedule, "execution_mode", "docker") or "docker",
        created_at=schedule.created_at,
        updated_at=schedule.updated_at,
    )


async def _attach_node_titles(
    db: AsyncSession, schedules: list[Schedule]
) -> list[ScheduleResponse]:
    if not schedules:
        return []
    # 收集全部可能用到的 node id
    all_ids: set[str] = set()
    per_schedule: list[list[str]] = []
    for s in schedules:
        nids = _get_node_ids(s)
        per_schedule.append(nids)
        all_ids.update(nids)
    rows = await db.execute(select(TreeNode).where(TreeNode.id.in_(all_ids)))
    titles = {n.id: n.name for n in rows.scalars()}
    out: list[ScheduleResponse] = []
    for s, nids in zip(schedules, per_schedule):
        primary_title = titles.get(s.node_id)
        all_titles = [titles.get(nid, nid) for nid in nids]
        out.append(_to_response(s, primary_title, node_titles=all_titles, node_ids=nids))
    return out


def _normalize_repeat_type(value: str) -> RepeatType:
    try:
        return RepeatType(value.upper())
    except (KeyError, ValueError):
        raise HTTPException(status_code=400, detail=f"repeat_type 不合法：{value}")


def _resolve_payload_nodes(payload: ScheduleCreate) -> list[str]:
    """從 payload 決定節點清單：優先用 node_ids，沒有才退化到 node_id。"""
    if payload.node_ids:
        seen: set[str] = set()
        out: list[str] = []
        for nid in payload.node_ids:
            if nid and nid not in seen:
                seen.add(nid)
                out.append(nid)
        if out:
            return out
    if payload.node_id:
        return [payload.node_id]
    return []


@router.get("/schedules", response_model=list[ScheduleResponse], tags=["F · 排程"])
async def list_schedules(
    project_id: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(Schedule).order_by(desc(Schedule.created_at))
    if project_id:
        stmt = stmt.where(Schedule.project_id == project_id)
    result = await db.execute(stmt)
    schedules = list(result.scalars())
    return await _attach_node_titles(db, schedules)


@router.post("/schedules", response_model=ScheduleResponse, status_code=201, tags=["F · 排程"])
async def create_schedule(payload: ScheduleCreate, db: AsyncSession = Depends(get_db)):
    nids = _resolve_payload_nodes(payload)
    if not nids:
        raise HTTPException(status_code=400, detail="請至少選擇一個節點")
    primary = nids[0]
    node = await db.get(TreeNode, primary)
    if node is None:
        raise HTTPException(status_code=404, detail="Node not found")

    schedule = Schedule(
        name=payload.name,
        node_id=primary,
        node_ids_json=json.dumps(nids, ensure_ascii=False),
        project_id=node.project_id,
        repeat_type=_normalize_repeat_type(payload.repeat_type),
        repeat_config=payload.repeat_config or None,
        next_run_at=payload.next_run_at,
        active=payload.active,
        execution_mode=(payload.execution_mode or "docker").lower(),
    )
    db.add(schedule)
    await db.flush()
    # 回傳含所有節點 title
    rows = await db.execute(select(TreeNode).where(TreeNode.id.in_(nids)))
    titles = {n.id: n.name for n in rows.scalars()}
    return _to_response(
        schedule, titles.get(primary), node_titles=[titles.get(i, i) for i in nids], node_ids=nids
    )


@router.get("/schedules/{schedule_id}", response_model=ScheduleResponse, tags=["F · 排程"])
async def get_schedule(schedule_id: str, db: AsyncSession = Depends(get_db)):
    schedule = await db.get(Schedule, schedule_id)
    if schedule is None:
        raise HTTPException(status_code=404, detail="Schedule not found")
    nids = _get_node_ids(schedule)
    rows = await db.execute(select(TreeNode).where(TreeNode.id.in_(nids)))
    titles = {n.id: n.name for n in rows.scalars()}
    return _to_response(
        schedule, titles.get(schedule.node_id),
        node_titles=[titles.get(i, i) for i in nids], node_ids=nids,
    )


@router.put("/schedules/{schedule_id}", response_model=ScheduleResponse, tags=["F · 排程"])
async def update_schedule(
    schedule_id: str, payload: ScheduleUpdate, db: AsyncSession = Depends(get_db)
):
    schedule = await db.get(Schedule, schedule_id)
    if schedule is None:
        raise HTTPException(status_code=404, detail="Schedule not found")

    if payload.name is not None:
        schedule.name = payload.name
    if payload.repeat_type is not None:
        schedule.repeat_type = _normalize_repeat_type(payload.repeat_type)
    if payload.repeat_config is not None:
        schedule.repeat_config = payload.repeat_config or None
    if payload.next_run_at is not None:
        schedule.next_run_at = payload.next_run_at
    if payload.active is not None:
        schedule.active = payload.active
    if payload.execution_mode is not None:
        schedule.execution_mode = (payload.execution_mode or "docker").lower()
    # 處理節點更新：node_ids 優先，其次單個 node_id
    new_nids: Optional[list[str]] = None
    if payload.node_ids is not None and payload.node_ids:
        new_nids = [n for n in payload.node_ids if n]
    elif payload.node_id:
        new_nids = [payload.node_id]
    if new_nids:
        schedule.node_id = new_nids[0]
        schedule.node_ids_json = json.dumps(new_nids, ensure_ascii=False)

    await db.flush()
    nids = _get_node_ids(schedule)
    rows = await db.execute(select(TreeNode).where(TreeNode.id.in_(nids)))
    titles = {n.id: n.name for n in rows.scalars()}
    return _to_response(
        schedule, titles.get(schedule.node_id),
        node_titles=[titles.get(i, i) for i in nids], node_ids=nids,
    )


@router.delete("/schedules/{schedule_id}", status_code=204, tags=["F · 排程"])
async def delete_schedule(schedule_id: str, db: AsyncSession = Depends(get_db)):
    schedule = await db.get(Schedule, schedule_id)
    if schedule is None:
        raise HTTPException(status_code=404, detail="Schedule not found")
    await db.delete(schedule)


@router.post("/schedules/{schedule_id}/trigger-now", response_model=ScheduleResponse, tags=["F · 排程"])
async def trigger_schedule_now(
    schedule_id: str,
    execution_mode: Optional[str] = Query(None, pattern="^(docker|local)$"),
    db: AsyncSession = Depends(get_db),
):
    """立即觸發一次，但不更動 next_run_at（排程仍會照原時間再次觸發）。

    execution_mode：若傳入（docker/local）就用傳入值；未傳則使用 schedule 儲存的 execution_mode。
    """
    schedule = await db.get(Schedule, schedule_id)
    if schedule is None:
        raise HTTPException(status_code=404, detail="Schedule not found")

    mode = (execution_mode or getattr(schedule, "execution_mode", None) or "docker").lower()
    report_id = await _trigger_schedule(db, schedule, execution_mode=mode)
    if report_id is None:
        raise HTTPException(
            status_code=400,
            detail="目標節點底下找不到任何 TESTCASE，無法觸發執行",
        )
    schedule.last_run_at = datetime.now()
    schedule.last_report_id = report_id
    await db.flush()
    nids = _get_node_ids(schedule)
    rows = await db.execute(select(TreeNode).where(TreeNode.id.in_(nids)))
    titles = {n.id: n.name for n in rows.scalars()}
    return _to_response(
        schedule, titles.get(schedule.node_id),
        node_titles=[titles.get(i, i) for i in nids], node_ids=nids,
    )
