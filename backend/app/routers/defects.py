"""Defect 缺陷管理 REST endpoints。"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.defect import Defect, DefectSeverity, DefectPriority, DefectStatus
from app.schemas.defect import DefectCreate, DefectResponse, DefectUpdate

router = APIRouter()


async def _next_code(db: AsyncSession, project_id: str) -> str:
    """產生下一個 BUG-NNN code（同專案內遞增）。"""
    result = await db.execute(
        select(func.count(Defect.id)).where(Defect.project_id == project_id)
    )
    n = (result.scalar_one_or_none() or 0) + 1
    return f"BUG-{n:03d}"


def _resolve_enum(enum_cls, val, default):
    if val is None:
        return default
    try:
        return enum_cls(val)
    except ValueError:
        return default


@router.get("/defects", response_model=list[DefectResponse], tags=["L · 缺陷管理"])
async def list_defects(
    project_id: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    severity: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(Defect).order_by(desc(Defect.created_at))
    if project_id:
        stmt = stmt.where(Defect.project_id == project_id)
    if status:
        stmt = stmt.where(Defect.status == DefectStatus(status))
    if severity:
        stmt = stmt.where(Defect.severity == DefectSeverity(severity))
    rows = (await db.execute(stmt)).scalars().all()
    return list(rows)


@router.post("/defects", response_model=DefectResponse, status_code=201, tags=["L · 缺陷管理"])
async def create_defect(payload: DefectCreate, db: AsyncSession = Depends(get_db)):
    code = payload.code or await _next_code(db, payload.project_id)
    defect = Defect(
        project_id=payload.project_id,
        code=code,
        title=payload.title,
        description=payload.description,
        steps_to_reproduce=payload.steps_to_reproduce,
        expected_result=payload.expected_result,
        actual_result=payload.actual_result,
        severity=_resolve_enum(DefectSeverity, payload.severity, DefectSeverity.MINOR),
        priority=_resolve_enum(DefectPriority, payload.priority, DefectPriority.P2),
        status=_resolve_enum(DefectStatus, payload.status, DefectStatus.NEW),
        reporter=payload.reporter,
        assignee=payload.assignee,
        linked_testcase_id=payload.linked_testcase_id,
        linked_report_id=payload.linked_report_id,
        external_url=payload.external_url,
    )
    db.add(defect)
    await db.flush()
    await db.refresh(defect)
    return defect


@router.get("/defects/{defect_id}", response_model=DefectResponse, tags=["L · 缺陷管理"])
async def get_defect(defect_id: str, db: AsyncSession = Depends(get_db)):
    d = await db.get(Defect, defect_id)
    if not d:
        raise HTTPException(404, "Defect not found")
    return d


@router.put("/defects/{defect_id}", response_model=DefectResponse, tags=["L · 缺陷管理"])
async def update_defect(defect_id: str, payload: DefectUpdate, db: AsyncSession = Depends(get_db)):
    d = await db.get(Defect, defect_id)
    if not d:
        raise HTTPException(404, "Defect not found")
    data = payload.model_dump(exclude_unset=True)
    for key, val in data.items():
        if key == "severity" and val is not None:
            d.severity = _resolve_enum(DefectSeverity, val, d.severity)
        elif key == "priority" and val is not None:
            d.priority = _resolve_enum(DefectPriority, val, d.priority)
        elif key == "status" and val is not None:
            new_status = _resolve_enum(DefectStatus, val, d.status)
            d.status = new_status
            # 進入 Closed 自動標記 closed_at
            if new_status == DefectStatus.CLOSED and d.closed_at is None:
                d.closed_at = datetime.utcnow()
            elif new_status != DefectStatus.CLOSED and d.closed_at is not None:
                d.closed_at = None
        else:
            setattr(d, key, val)
    await db.flush()
    await db.refresh(d)
    return d


@router.delete("/defects/{defect_id}", status_code=204, tags=["L · 缺陷管理"])
async def delete_defect(defect_id: str, db: AsyncSession = Depends(get_db)):
    d = await db.get(Defect, defect_id)
    if not d:
        raise HTTPException(404, "Defect not found")
    await db.delete(d)
    await db.flush()


@router.get("/defects/stats/summary", tags=["L · 缺陷管理"])
async def defects_summary(
    project_id: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """供「測試看板」的 KPI cards 與 Kanban 列數使用。"""
    stmt = select(Defect.status, Defect.severity)
    if project_id:
        stmt = stmt.where(Defect.project_id == project_id)
    rows = (await db.execute(stmt)).all()
    by_status: dict[str, int] = {}
    by_severity: dict[str, int] = {}
    for status_val, severity_val in rows:
        s = status_val.value if hasattr(status_val, "value") else str(status_val)
        sv = severity_val.value if hasattr(severity_val, "value") else str(severity_val)
        by_status[s] = by_status.get(s, 0) + 1
        by_severity[sv] = by_severity.get(sv, 0) + 1
    return {
        "total": sum(by_status.values()),
        "by_status": by_status,
        "by_severity": by_severity,
    }
