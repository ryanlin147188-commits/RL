"""Celery email task — wraps the sync send_email_sync helper.

Runs in the celery worker (sync DB session via app.db.sync_session).
FastAPI handlers should NOT call send_email_sync directly; they should
go through ``app.services.notification_dispatch.notify`` which enqueues
this task and returns immediately.

Retry policy:
    * EmailNotConfigured  — terminal, no retry. The org just doesn't have
      SMTP set up; logging it is enough.
    * EmailSendFailed     — transient (connection refused, timeout,
      smtp 4xx). Retry up to 3 times with 60s backoff.
    * Unexpected exception — re-raise, Celery default retry kicks in.
"""
from __future__ import annotations

import logging

from celery.utils.log import get_task_logger

from app.db.sync_session import SessionLocal
from app.services.email_service import (
    EmailNotConfigured,
    EmailSendFailed,
    send_email_sync,
)
from tasks.celery_app import celery_app

logger = get_task_logger(__name__)


@celery_app.task(
    bind=True,
    name="tasks.email_tasks.send_email",
    max_retries=3,
    default_retry_delay=60,
    autoretry_for=(EmailSendFailed,),
    retry_backoff=True,
    retry_jitter=True,
)
def send_email_task(
    self,
    *,
    to: str,
    subject: str,
    html_body: str,
    text_body: str,
    organization_id: str | None = None,
) -> dict:
    """Send one email. Errors are split:
      - EmailNotConfigured  -> log + ack (don't retry; nothing to retry)
      - EmailSendFailed     -> raise so autoretry_for kicks in
      - other Exception     -> propagate
    """
    with SessionLocal() as db:
        try:
            send_email_sync(
                db=db,
                to=to,
                subject=subject,
                html_body=html_body,
                text_body=text_body,
                organization_id=organization_id,
            )
            return {"sent": True, "to": to}
        except EmailNotConfigured as exc:
            logger.warning("email not configured (org=%s): %s", organization_id, exc)
            return {"sent": False, "reason": "not_configured"}
        # EmailSendFailed propagates -> Celery autoretry_for handles it.
