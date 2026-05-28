"""PendingAction CRUD — Phase 1c-2 二次確認流程。

僅做 DB / 狀態轉換;真實 dispatch / follow-up chat 由 ``agent_service`` 統籌。
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.pending_action import (
    PENDING_STATUS_APPROVED,
    PENDING_STATUS_EXPIRED,
    PENDING_STATUS_PENDING,
    PENDING_STATUS_REJECTED,
    PendingAction,
)


class PendingActionError(Exception):
    """用 message 直接表達錯誤;router 轉成 HTTPException。"""


class PendingActionNotFound(PendingActionError):
    pass


class PendingActionAlreadyResolved(PendingActionError):
    pass


class PendingActionExpired(PendingActionError):
    pass


async def create(
    db: AsyncSession,
    *,
    session_id: str,
    user_id: str,
    tool_call_id: str,
    tool_name: str,
    arguments: Optional[dict[str, Any]],
    summary: Optional[str] = None,
) -> PendingAction:
    row = PendingAction(
        id=str(uuid.uuid4()),
        session_id=session_id,
        user_id=user_id,
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        arguments=arguments,
        summary=summary,
    )
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return row


async def list_for_session(
    db: AsyncSession,
    *,
    session_id: str,
    user_id: str,
    status: Optional[str] = None,
    limit: int = 50,
) -> list[PendingAction]:
    """列該 session 的 pending actions(僅 user 自己的)。"""
    stmt = (
        select(PendingAction)
        .where(
            PendingAction.session_id == session_id,
            PendingAction.user_id == user_id,
        )
        .order_by(desc(PendingAction.created_at))
        .limit(limit)
    )
    if status is not None:
        stmt = stmt.where(PendingAction.status == status)
    return list((await db.execute(stmt)).scalars().all())


async def get_for_user(
    db: AsyncSession, *, action_id: str, user_id: str
) -> Optional[PendingAction]:
    """讀單筆,但只回該 user 自己的(防 IDOR — 別人不能 approve 你的 pending)。"""
    stmt = select(PendingAction).where(
        PendingAction.id == action_id, PendingAction.user_id == user_id
    )
    return (await db.execute(stmt)).scalar_one_or_none()


def _check_resolvable(action: PendingAction) -> None:
    """approve / reject 前共用的狀態檢查。"""
    if action.status != PENDING_STATUS_PENDING:
        raise PendingActionAlreadyResolved(
            f"PendingAction 已是 {action.status},無法再次處理"
        )
    if action.expires_at <= datetime.utcnow():
        raise PendingActionExpired(
            f"PendingAction 已過期({action.expires_at.isoformat()})"
        )


async def mark_approved(db: AsyncSession, action: PendingAction) -> PendingAction:
    _check_resolvable(action)
    action.status = PENDING_STATUS_APPROVED
    action.resolved_at = datetime.utcnow()
    await db.flush()
    return action


async def mark_rejected(db: AsyncSession, action: PendingAction) -> PendingAction:
    _check_resolvable(action)
    action.status = PENDING_STATUS_REJECTED
    action.resolved_at = datetime.utcnow()
    await db.flush()
    return action


async def mark_expired_if_due(
    db: AsyncSession, action: PendingAction
) -> PendingAction:
    """如果還是 pending 且已過期就標 expired。等使用者 approve 時若已過期,
    可呼這個一併更新狀態 + 給前端正確的展示。"""
    if (
        action.status == PENDING_STATUS_PENDING
        and action.expires_at <= datetime.utcnow()
    ):
        action.status = PENDING_STATUS_EXPIRED
        action.resolved_at = datetime.utcnow()
        await db.flush()
    return action
