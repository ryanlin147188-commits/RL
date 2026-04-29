"""Test Milestone 測試時程 REST endpoints。"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.auth.scope import (
    ensure_project_in_scope,
    ensure_project_writable,
    scope_by_project,
)
from app.database import get_db
from app.models.test_milestone import MilestoneStatus, TestMilestone
from app.models.user import User
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
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(TestMilestone).order_by(TestMilestone.start_date)
    if project_id:
        stmt = stmt.where(TestMilestone.project_id == project_id)
    stmt = scope_by_project(stmt, TestMilestone, user)
    rows = (await db.execute(stmt)).scalars().all()
    return list(rows)


@router.post("/milestones", response_model=MilestoneResponse, status_code=201, tags=["M · 測試時程"])
async def create_milestone(
    payload: MilestoneCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await ensure_project_writable(db, payload.project_id, user)
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
async def get_milestone(
    milestone_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    m = await db.get(TestMilestone, milestone_id)
    await ensure_project_in_scope(
        db, m.project_id if m else None, user, not_found_detail="Milestone not found"
    )
    return m


@router.put("/milestones/{milestone_id}", response_model=MilestoneResponse, tags=["M · 測試時程"])
async def update_milestone(
    milestone_id: str,
    payload: MilestoneUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    m = await db.get(TestMilestone, milestone_id)
    await ensure_project_in_scope(
        db, m.project_id if m else None, user, not_found_detail="Milestone not found"
    )
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
async def delete_milestone(
    milestone_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    m = await db.get(TestMilestone, milestone_id)
    await ensure_project_in_scope(
        db, m.project_id if m else None, user, not_found_detail="Milestone not found"
    )
    await db.delete(m)
    await db.flush()
