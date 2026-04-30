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
