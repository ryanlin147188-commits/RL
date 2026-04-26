"""Test Milestone 測試時程 REST endpoints。"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.test_milestone import MilestoneStatus, TestMilestone
from app.schemas.test_milestone import (
    MilestoneCreate,
    MilestoneResponse,
    MilestoneUpdate,
)

router = APIRouter()


def _resolve_status(val, default):
    if val is None:
        return default
    try:
        return MilestoneStatus(val)
    except ValueError:
        return default


@router.get("/milestones", response_model=list[MilestoneResponse], tags=["M · 測試時程"])
async def list_milestones(
    project_id: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(TestMilestone).order_by(TestMilestone.start_date)
    if project_id:
        stmt = stmt.where(TestMilestone.project_id == project_id)
    rows = (await db.execute(stmt)).scalars().all()
    return list(rows)


@router.post("/milestones", response_model=MilestoneResponse, status_code=201, tags=["M · 測試時程"])
async def create_milestone(payload: MilestoneCreate, db: AsyncSession = Depends(get_db)):
    if payload.end_date < payload.start_date:
        raise HTTPException(400, "end_date 不能早於 start_date")
    m = TestMilestone(
        project_id=payload.project_id,
        name=payload.name,
        description=payload.description,
        start_date=payload.start_date,
        end_date=payload.end_date,
        status=_resolve_status(payload.status, MilestoneStatus.PLANNED),
        owner=payload.owner,
        color=payload.color,
        linked_test_round_id=payload.linked_test_round_id,
        linked_test_plan_id=payload.linked_test_plan_id,
    )
    db.add(m)
    await db.flush()
    await db.refresh(m)
    return m


@router.get("/milestones/{milestone_id}", response_model=MilestoneResponse, tags=["M · 測試時程"])
async def get_milestone(milestone_id: str, db: AsyncSession = Depends(get_db)):
    m = await db.get(TestMilestone, milestone_id)
    if not m:
        raise HTTPException(404, "Milestone not found")
    return m


@router.put("/milestones/{milestone_id}", response_model=MilestoneResponse, tags=["M · 測試時程"])
async def update_milestone(
    milestone_id: str, payload: MilestoneUpdate, db: AsyncSession = Depends(get_db)
):
    m = await db.get(TestMilestone, milestone_id)
    if not m:
        raise HTTPException(404, "Milestone not found")
    data = payload.model_dump(exclude_unset=True)
    for key, val in data.items():
        if key == "status" and val is not None:
            m.status = _resolve_status(val, m.status)
        else:
            setattr(m, key, val)
    if m.end_date < m.start_date:
        raise HTTPException(400, "end_date 不能早於 start_date")
    await db.flush()
    await db.refresh(m)
    return m


@router.delete("/milestones/{milestone_id}", status_code=204, tags=["M · 測試時程"])
async def delete_milestone(milestone_id: str, db: AsyncSession = Depends(get_db)):
    m = await db.get(TestMilestone, milestone_id)
    if not m:
        raise HTTPException(404, "Milestone not found")
    await db.delete(m)
    await db.flush()
