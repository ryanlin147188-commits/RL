"""Audit log REST endpoints — 唯讀；只有 superuser 能查看。

筆數可能很大，預設限制 100；提供基本過濾參數。
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.database import get_db
from app.models.audit_log import AuditLog
from app.models.user import User

router = APIRouter()


def _require_superuser(user: User) -> None:
    if not user.is_superuser:
        raise HTTPException(403, "需要 superuser 權限才能查看審計紀錄")


@router.get("/audit-logs", tags=["W · 審計"])
async def list_audit_logs(
    username: Optional[str] = Query(None),
    entity_type: Optional[str] = Query(None),
    entity_id: Optional[str] = Query(None),
    method: Optional[str] = Query(None),
    status_min: Optional[int] = Query(None),
    status_max: Optional[int] = Query(None),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _require_superuser(user)
    stmt = select(AuditLog).order_by(desc(AuditLog.created_at))
    if username:
        stmt = stmt.where(AuditLog.username == username)
    if entity_type:
        stmt = stmt.where(AuditLog.entity_type == entity_type)
    if entity_id:
        stmt = stmt.where(AuditLog.entity_id == entity_id)
    if method:
        stmt = stmt.where(AuditLog.method == method.upper())
    if status_min is not None:
        stmt = stmt.where(AuditLog.status_code >= status_min)
    if status_max is not None:
        stmt = stmt.where(AuditLog.status_code <= status_max)
    stmt = stmt.limit(limit).offset(offset)
    rows = (await db.execute(stmt)).scalars().all()
    return [
        {
            "id": r.id,
            "organization_id": r.organization_id,
            "username": r.username,
            "method": r.method,
            "path": r.path,
            "entity_type": r.entity_type,
            "entity_id": r.entity_id,
            "status_code": r.status_code,
            "duration_ms": r.duration_ms,
            "ip_address": r.ip_address,
            "user_agent": r.user_agent,
            "request_query": r.request_query,
            "created_at": r.created_at,
        }
        for r in rows
    ]


@router.get("/audit-logs/stats", tags=["W · 審計"])
async def audit_log_stats(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """頂部 KPI 用：總筆數、各 status code 區段、最近 24 小時筆數。"""
    from sqlalchemy import func
    _require_superuser(user)
    total = (await db.execute(select(func.count(AuditLog.id)))).scalar_one() or 0
    success = (
        await db.execute(
            select(func.count(AuditLog.id)).where(AuditLog.status_code < 400)
        )
    ).scalar_one() or 0
    client_err = (
        await db.execute(
            select(func.count(AuditLog.id)).where(
                AuditLog.status_code >= 400, AuditLog.status_code < 500
            )
        )
    ).scalar_one() or 0
    server_err = (
        await db.execute(
            select(func.count(AuditLog.id)).where(AuditLog.status_code >= 500)
        )
    ).scalar_one() or 0
    from datetime import timedelta
    since = datetime.utcnow() - timedelta(hours=24)
    last_24h = (
        await db.execute(
            select(func.count(AuditLog.id)).where(AuditLog.created_at >= since)
        )
    ).scalar_one() or 0
    return {
        "total": total,
        "success_2xx_3xx": success,
        "client_error_4xx": client_err,
        "server_error_5xx": server_err,
        "last_24h": last_24h,
    }
