"""Test Plan 測試計畫 REST endpoints。"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.auth.scope import (
    ensure_project_in_scope,
    ensure_project_writable,
    scope_by_project,
)
from app.common import Pagination
from app.database import get_db
from app.models.test_plan import TestPlan, TestPlanStatus
from app.models.user import User
from app.schemas.test_plan import TestPlanCreate, TestPlanResponse, TestPlanUpdate

router = APIRouter()


async def _next_code(db: AsyncSession, project_id: str) -> str:
    result = await db.execute(
        select(func.count(TestPlan.id)).where(TestPlan.project_id == project_id)
    )
    n = (result.scalar_one_or_none() or 0) + 1
    return f"TP-{n:03d}"


def _resolve_status(val, default):
    if val is None:
        return default
    try:
        return TestPlanStatus(val)
    except ValueError:
        return default


@router.get("/plans", response_model=list[TestPlanResponse], tags=["N · 測試計畫"])
async def list_plans(
    project_id: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    page: Pagination = Depends(Pagination.from_query),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(TestPlan).order_by(desc(TestPlan.created_at))
    if project_id:
        stmt = stmt.where(TestPlan.project_id == project_id)
    if status:
        stmt = stmt.where(TestPlan.status == TestPlanStatus(status))
    stmt = scope_by_project(stmt, TestPlan, user)
    stmt = page.apply(stmt)
    rows = (await db.execute(stmt)).scalars().all()
    return list(rows)


@router.post("/plans", response_model=TestPlanResponse, status_code=201, tags=["N · 測試計畫"])
async def create_plan(
    payload: TestPlanCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await ensure_project_writable(db, payload.project_id, user)
    code = payload.code or await _next_code(db, payload.project_id)
    plan = TestPlan(
        project_id=payload.project_id,
        code=code,
        title=payload.title,
        version=payload.version,
        scope_in_text=payload.scope_in_text,
        scope_out_text=payload.scope_out_text,
        test_strategy_text=payload.test_strategy_text,
        resources_text=payload.resources_text,
        schedule_text=payload.schedule_text,
        risks_text=payload.risks_text,
        entry_criteria_json=payload.entry_criteria_json,
        exit_criteria_json=payload.exit_criteria_json,
        approvals_json=payload.approvals_json,
        status=_resolve_status(payload.status, TestPlanStatus.DRAFT),
        owner=payload.owner,
    )
    db.add(plan)
    await db.flush()
    await db.refresh(plan)
    return plan


@router.get("/plans/{plan_id}", response_model=TestPlanResponse, tags=["N · 測試計畫"])
async def get_plan(
    plan_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    p = await db.get(TestPlan, plan_id)
    await ensure_project_in_scope(
        db, p.project_id if p else None, user, not_found_detail="Test plan not found"
    )
    return p


@router.put("/plans/{plan_id}", response_model=TestPlanResponse, tags=["N · 測試計畫"])
async def update_plan(
    plan_id: str,
    payload: TestPlanUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    p = await db.get(TestPlan, plan_id)
    await ensure_project_in_scope(
        db, p.project_id if p else None, user, not_found_detail="Test plan not found"
    )
    data = payload.model_dump(exclude_unset=True)
    for key, val in data.items():
        if key == "status" and val is not None:
            new_status = _resolve_status(val, p.status)
            p.status = new_status
            if new_status == TestPlanStatus.APPROVED and p.approved_at is None:
                p.approved_at = datetime.utcnow()
        else:
            setattr(p, key, val)
    await db.flush()
    await db.refresh(p)
    return p


@router.delete("/plans/{plan_id}", status_code=204, tags=["N · 測試計畫"])
async def delete_plan(
    plan_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    p = await db.get(TestPlan, plan_id)
    await ensure_project_in_scope(
        db, p.project_id if p else None, user, not_found_detail="Test plan not found"
    )
    await db.delete(p)
    await db.flush()
