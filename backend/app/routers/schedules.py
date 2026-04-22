"""排程（Schedule）REST endpoints。

- GET    /schedules                      列出所有排程（可選 project_id 過濾）
- POST   /schedules                      建立排程
- GET    /schedules/{id}                 取得單一排程
- PUT    /schedules/{id}                 更新排程（任一欄位）
- DELETE /schedules/{id}                 刪除排程
- POST   /schedules/{id}/trigger-now     立即觸發一次（不影響 next_run_at）
"""
from __future__ import annotations

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


def _to_response(schedule: Schedule, node_title: Optional[str]) -> ScheduleResponse:
    return ScheduleResponse(
        id=schedule.id,
        name=schedule.name,
        node_id=schedule.node_id,
        project_id=schedule.project_id,
        node_title=node_title,
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
    node_ids = {s.node_id for s in schedules}
    rows = await db.execute(select(TreeNode).where(TreeNode.id.in_(node_ids)))
    titles = {n.id: n.name for n in rows.scalars()}
    return [_to_response(s, titles.get(s.node_id)) for s in schedules]


def _normalize_repeat_type(value: str) -> RepeatType:
    try:
        return RepeatType(value.upper())
    except (KeyError, ValueError):
        raise HTTPException(status_code=400, detail=f"repeat_type 不合法：{value}")


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
    node = await db.get(TreeNode, payload.node_id)
    if node is None:
        raise HTTPException(status_code=404, detail="Node not found")

    schedule = Schedule(
        name=payload.name,
        node_id=payload.node_id,
        project_id=node.project_id,
        repeat_type=_normalize_repeat_type(payload.repeat_type),
        repeat_config=payload.repeat_config or None,
        next_run_at=payload.next_run_at,
        active=payload.active,
        execution_mode=(payload.execution_mode or "docker").lower(),
    )
    db.add(schedule)
    await db.flush()
    return _to_response(schedule, node.name)


@router.get("/schedules/{schedule_id}", response_model=ScheduleResponse, tags=["F · 排程"])
async def get_schedule(schedule_id: str, db: AsyncSession = Depends(get_db)):
    schedule = await db.get(Schedule, schedule_id)
    if schedule is None:
        raise HTTPException(status_code=404, detail="Schedule not found")
    node = await db.get(TreeNode, schedule.node_id)
    return _to_response(schedule, node.name if node else None)


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

    await db.flush()
    node = await db.get(TreeNode, schedule.node_id)
    return _to_response(schedule, node.name if node else None)


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
    node = await db.get(TreeNode, schedule.node_id)
    return _to_response(schedule, node.name if node else None)
