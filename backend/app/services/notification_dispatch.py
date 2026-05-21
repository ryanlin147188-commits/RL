"""Single entry point for sending notifications.

Routers should call ``notify(...)`` and let this module handle:

  1. Always inserting a Notification row (the in-app inbox bell).
  2. Looking up the recipient's NotificationPreference; if email is
     enabled for this event_key, enqueueing a send_email_task.
  3. Resolving the recipient's email address from the User table.

Failure handling: any error inside the dispatch is logged but never
raised — a misconfigured notification pipeline must NOT break the
business action that triggered it (e.g. an approve POST shouldn't 500
because the SMTP host is wrong).

Email delivery itself is async via Celery; this function returns as
soon as the in-app row is staged in the session.
"""
from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.notification import Notification
from app.models.notification_preference import NotificationPreference
from app.models.user import User

log = logging.getLogger(__name__)


async def _email_enabled_for(
    db: AsyncSession, *, username: str, event_key: str
) -> bool:
    """True iff the user's NotificationPreference has email=True for this event.

    Looks up the per-user row first; falls back to the global default
    (username IS NULL) so admins can set org-wide opt-in defaults without
    every user touching their settings."""
    pref = (
        await db.execute(
            select(NotificationPreference).where(NotificationPreference.username == username)
        )
    ).scalar_one_or_none()
    if pref is None:
        pref = (
            await db.execute(
                select(NotificationPreference).where(NotificationPreference.username.is_(None))
            )
        ).scalar_one_or_none()
    if pref is None or not pref.events_json:
        return False
    ev = pref.events_json.get(event_key) or {}
    return bool(ev.get("email"))


async def _lookup_email(db: AsyncSession, username: str) -> Optional[str]:
    user = (
        await db.execute(select(User).where(User.username == username))
    ).scalar_one_or_none()
    return user.email if (user and user.email) else None


async def notify_broadcast(
    *,
    db: AsyncSession,
    event_key: str,
    organization_id: Optional[str],
    title: str,
    body: Optional[str] = None,
    level: str = "info",
    link: Optional[str] = None,
    related_entity_type: Optional[str] = None,
    related_entity_id: Optional[str] = None,
    required_permission: Optional[str] = None,
) -> int:
    """事件沒有特定 recipient(例:run.started / schedule.fired / 沒指派的
    review.submitted)時,把通知 fan-out 給該 org 的所有 active 使用者 —
    notify() 內部會檢查 NotificationPreference,所以只有真的有訂閱
    email 的人會收到信。

    ``required_permission`` 給 review.submitted broadcast 用 — 只 fan-out 給
    具該權限的使用者(例如 ``review.manage``)以免一般使用者被沒打中的
    review 通知洗版。
    """
    from app.models.role import Role

    stmt = select(User).where(User.is_active.is_(True))
    if organization_id is not None:
        stmt = stmt.where(User.organization_id == organization_id)
    users = (await db.execute(stmt)).scalars().all()
    if not users:
        return 0

    role_perms_cache: dict[str, list[str]] = {}

    async def _has_perm(u: User, perm: str) -> bool:
        if u.is_superuser:
            return True
        if not u.role_id:
            return False
        perms = role_perms_cache.get(u.role_id)
        if perms is None:
            role = await db.get(Role, u.role_id)
            perms = list((role.permissions_json if role else None) or [])
            role_perms_cache[u.role_id] = perms
        return perm in perms

    sent = 0
    for u in users:
        if required_permission and not await _has_perm(u, required_permission):
            continue
        await notify(
            db=db,
            event_key=event_key,
            recipient=u.username,
            title=title,
            body=body,
            level=level,
            link=link,
            related_entity_type=related_entity_type,
            related_entity_id=related_entity_id,
            organization_id=organization_id,
        )
        sent += 1
    return sent


def notify_broadcast_sync(
    *,
    sync_db,
    event_key: str,
    organization_id: Optional[str],
    title: str,
    body: Optional[str] = None,
    level: str = "info",
    link: Optional[str] = None,
    related_entity_type: Optional[str] = None,
    related_entity_id: Optional[str] = None,
) -> int:
    """Sync-context broadcast — celery worker / 同步 SessionLocal 用。
    語意同 notify_broadcast() 但走純同步 SQLAlchemy session,並直接
    enqueue send_email_task.delay()(寫 in-app row + 排 celery email)。
    永不 raise,失敗只記 log。"""
    try:
        from sqlalchemy.orm import Session as _SyncSession  # noqa: F401  (型別提示)
        sel_users = select(User).where(User.is_active.is_(True))
        if organization_id is not None:
            sel_users = sel_users.where(User.organization_id == organization_id)
        users = sync_db.execute(sel_users).scalars().all()
        if not users:
            return 0

        # 預先撈 prefs(per-user + global default),用 dict 避免 N+1
        sel_prefs = select(NotificationPreference)
        prefs_rows = sync_db.execute(sel_prefs).scalars().all()
        per_user_pref: dict[str, NotificationPreference] = {}
        global_pref: Optional[NotificationPreference] = None
        for p in prefs_rows:
            if p.username is None:
                global_pref = p
            else:
                per_user_pref[p.username] = p

        sent = 0
        for u in users:
            pref = per_user_pref.get(u.username) or global_pref
            ev = ((pref.events_json or {}).get(event_key) if pref else None) or {}
            # 1) 永遠寫一筆 in-app
            sync_db.add(Notification(
                organization_id=organization_id,
                recipient=u.username,
                event_key=event_key,
                level=level,
                title=title,
                body=body,
                link=link,
                related_entity_type=related_entity_type,
                related_entity_id=related_entity_id,
            ))
            # 2) 若使用者訂閱 email + 有 email on file → 排 celery 任務
            if not ev.get("email") or not u.email:
                continue
            try:
                from app.services.email_service import render_notification_email
                from tasks.email_tasks import send_email_task
                html_body, text_body = render_notification_email(
                    title=title, body=body or "", link=link,
                )
                send_email_task.delay(
                    to=u.email,
                    subject=title,
                    html_body=html_body,
                    text_body=text_body,
                    organization_id=organization_id,
                )
                sent += 1
            except Exception:
                log.exception("notify_broadcast_sync: email enqueue failed for %s", u.username)
        sync_db.commit()
        return sent
    except Exception:
        log.exception("notify_broadcast_sync failed: event=%s org=%s", event_key, organization_id)
        try:
            sync_db.rollback()
        except Exception:
            pass
        return 0


async def notify(
    *,
    db: AsyncSession,
    event_key: str,
    recipient: str,
    title: str,
    body: Optional[str] = None,
    level: str = "info",
    link: Optional[str] = None,
    related_entity_type: Optional[str] = None,
    related_entity_id: Optional[str] = None,
    organization_id: Optional[str] = None,
) -> None:
    """Stage a Notification row and (if the recipient has email enabled
    for this event_key) queue an email. Never raises."""
    if not recipient:
        return
    try:
        # 1) In-app row -- always created so the bell badge works regardless
        #    of email config state.
        row = Notification(
            organization_id=organization_id,
            recipient=recipient,
            event_key=event_key,
            level=level,
            title=title,
            body=body,
            link=link,
            related_entity_type=related_entity_type,
            related_entity_id=related_entity_id,
        )
        db.add(row)

        # 2) Email enqueue if user opted in for this event.
        if not await _email_enabled_for(db, username=recipient, event_key=event_key):
            return
        to_addr = await _lookup_email(db, recipient)
        if not to_addr:
            log.info(
                "notify: %s has email enabled for %s but no email on file",
                recipient, event_key,
            )
            return

        # Render template + enqueue Celery task. Import locally so the
        # FastAPI process doesn't hard-depend on Celery client at module
        # load time (test envs without Redis stay quiet).
        from app.services.email_service import render_notification_email
        from tasks.email_tasks import send_email_task

        html_body, text_body = render_notification_email(
            title=title, body=body or "", link=link,
        )
        send_email_task.delay(
            to=to_addr,
            subject=title,
            html_body=html_body,
            text_body=text_body,
            organization_id=organization_id,
        )
    except Exception:  # noqa: BLE001
        log.exception("notify dispatch failed for event=%s recipient=%s", event_key, recipient)
