"""TestSchedule(測試時程)REST endpoints。

CRUD + list by project,跟 schedules(cron)拆開避免命名衝突 → URL 用
``/api/test-schedules`` 而非 ``/api/schedules``。
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.auth.permissions import require_permission
from app.auth.permissions_catalog import P
from app.auth.tenant import TenantQuery
from app.database import get_db
from app.models.test_schedule import TestSchedule, TestScheduleStatus
from app.models.user import User
from app.schemas.test_schedule import (
    TestScheduleCreate,
    TestScheduleResponse,
    TestScheduleUpdate,
)

router = APIRouter()


@router.get(
    "/test-schedules",
    response_model=List[TestScheduleResponse],
    tags=["AD · 測試時程"],
)
async def list_test_schedules(
    project_id: Optional[str] = Query(None),
    status: Optional[TestScheduleStatus] = Query(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    stmt = TenantQuery.for_(TestSchedule).order_by(TestSchedule.start_date)
    if project_id:
        stmt = stmt.where(TestSchedule.project_id == project_id)
    if status:
        stmt = stmt.where(TestSchedule.status == status)
    rows = (await db.execute(stmt)).scalars().all()
    return list(rows)


@router.get(
    "/test-schedules/{schedule_id}",
    response_model=TestScheduleResponse,
    tags=["AD · 測試時程"],
)
async def get_test_schedule(
    schedule_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    sched = (await db.execute(
        TenantQuery.for_(TestSchedule).where(TestSchedule.id == schedule_id)
    )).scalar_one_or_none()
    if sched is None:
        raise HTTPException(404, "TestSchedule not found")
    return sched


@router.post(
    "/test-schedules",
    response_model=TestScheduleResponse,
    status_code=201,
    tags=["AD · 測試時程"],
)
async def create_test_schedule(
    payload: TestScheduleCreate,
    user: User = Depends(require_permission(P.TESTCASE_WRITE)),
    db: AsyncSession = Depends(get_db),
):
    if payload.end_date < payload.start_date:
        raise HTTPException(422, "end_date 必須晚於或等於 start_date")
    sched = TestSchedule(
        id=str(uuid.uuid4()),
        project_id=payload.project_id,
        name=payload.name,
        description=payload.description,
        start_date=payload.start_date,
        end_date=payload.end_date,
        status=payload.status,
        color=payload.color,
        progress=payload.progress,
        assigned_to=payload.assigned_to,
        linked_target_type=payload.linked_target_type,
        linked_target_id=payload.linked_target_id,
    )
    db.add(sched)
    await db.commit()
    await db.refresh(sched)
    return sched


@router.patch(
    "/test-schedules/{schedule_id}",
    response_model=TestScheduleResponse,
    tags=["AD · 測試時程"],
)
async def update_test_schedule(
    schedule_id: str,
    payload: TestScheduleUpdate,
    user: User = Depends(require_permission(P.TESTCASE_WRITE)),
    db: AsyncSession = Depends(get_db),
):
    sched = (await db.execute(
        TenantQuery.for_(TestSchedule).where(TestSchedule.id == schedule_id)
    )).scalar_one_or_none()
    if sched is None:
        raise HTTPException(404, "TestSchedule not found")
    data = payload.model_dump(exclude_unset=True)
    for k, v in data.items():
        setattr(sched, k, v)
    if sched.end_date < sched.start_date:
        raise HTTPException(422, "end_date 必須晚於或等於 start_date")
    sched.updated_at = datetime.utcnow()
    await db.commit()
    await db.refresh(sched)
    return sched


@router.delete(
    "/test-schedules/{schedule_id}",
    status_code=204,
    tags=["AD · 測試時程"],
)
async def delete_test_schedule(
    schedule_id: str,
    user: User = Depends(require_permission(P.TESTCASE_DELETE)),
    db: AsyncSession = Depends(get_db),
):
    sched = (await db.execute(
        TenantQuery.for_(TestSchedule).where(TestSchedule.id == schedule_id)
    )).scalar_one_or_none()
    if sched is None:
        raise HTTPException(404, "TestSchedule not found")
    await db.execute(delete(TestSchedule).where(TestSchedule.id == schedule_id))
    await db.commit()
