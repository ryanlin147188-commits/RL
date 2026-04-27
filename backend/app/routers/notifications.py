"""Notification REST endpoints — 站內通知收件匣。

提供 top bar 鈴鐺所需的所有 API：
- list / unread-count（讀）
- POST {id}/read / read-all（已讀）
- DELETE {id} / DELETE all（清除）
- POST /（建立；用於系統內部事件，例：缺陷被指派、執行失敗）

權限：所有端點都用 `get_current_user`，使用者只看得到 recipient == 自己 username 的通知。
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import delete as sa_delete, desc, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.database import get_db
from app.models.notification import Notification
from app.models.user import User
from app.schemas.notification import (
    NotificationCreate,
    NotificationResponse,
    UnreadCountResponse,
)

router = APIRouter()


@router.get(
    "/notifications",
    response_model=list[NotificationResponse],
    tags=["Y · 通知"],
)
async def list_notifications(
    unread_only: bool = Query(False, description="只回傳未讀"),
    limit: int = Query(20, ge=1, le=100),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    stmt = (
        select(Notification)
        .where(Notification.recipient == user.username)
        .order_by(desc(Notification.created_at))
        .limit(limit)
    )
    if unread_only:
        stmt = stmt.where(Notification.is_read.is_(False))
    rows = (await db.execute(stmt)).scalars().all()
    return list(rows)


@router.get(
    "/notifications/unread-count",
    response_model=UnreadCountResponse,
    tags=["Y · 通知"],
)
async def unread_count(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    n = (
        await db.execute(
            select(func.count())
            .select_from(Notification)
            .where(
                Notification.recipient == user.username,
                Notification.is_read.is_(False),
            )
        )
    ).scalar_one() or 0
    return UnreadCountResponse(count=int(n))


@router.post(
    "/notifications",
    response_model=NotificationResponse,
    status_code=201,
    tags=["Y · 通知"],
)
async def create_notification(
    payload: NotificationCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """建立一筆通知（給 recipient）。組織歸屬以呼叫者 organization 為主。"""
    n = Notification(
        organization_id=user.organization_id,
        recipient=payload.recipient,
        title=payload.title,
        body=payload.body,
        level=payload.level,
        event_key=payload.event_key,
        link=payload.link,
        related_entity_type=payload.related_entity_type,
        related_entity_id=payload.related_entity_id,
    )
    db.add(n)
    await db.flush()
    await db.refresh(n)
    return n


@router.post(
    "/notifications/{notif_id}/read",
    response_model=NotificationResponse,
    tags=["Y · 通知"],
)
async def mark_read(
    notif_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    n = await db.get(Notification, notif_id)
    if not n or n.recipient != user.username:
        raise HTTPException(404, "Notification not found")
    if not n.is_read:
        n.is_read = True
        n.read_at = datetime.utcnow()
        await db.flush()
    await db.refresh(n)
    return n


@router.post("/notifications/read-all", tags=["Y · 通知"])
async def mark_all_read(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    now = datetime.utcnow()
    result = await db.execute(
        update(Notification)
        .where(
            Notification.recipient == user.username,
            Notification.is_read.is_(False),
        )
        .values(is_read=True, read_at=now)
    )
    return {"updated": result.rowcount or 0}


@router.delete(
    "/notifications/{notif_id}", status_code=204, tags=["Y · 通知"]
)
async def delete_notification(
    notif_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    n = await db.get(Notification, notif_id)
    if not n or n.recipient != user.username:
        raise HTTPException(404, "Notification not found")
    await db.delete(n)
    await db.flush()


@router.delete("/notifications", status_code=204, tags=["Y · 通知"])
async def clear_all(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """清除當前使用者的所有通知。"""
    await db.execute(
        sa_delete(Notification).where(Notification.recipient == user.username)
    )
    await db.flush()
