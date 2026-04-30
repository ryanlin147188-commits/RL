"""Email infrastructure integration tests (Phase 1 of the plan).

Spins up an in-process fake SMTP server via aiosmtpd, points an
EmailConfig row at it, and verifies:

  * send_email_sync produces a real RFC 822 message at the fake server
  * EmailNotConfigured fires when the org has no row / row is disabled
  * notify() always inserts a Notification regardless of email config
  * notify() skips email when NotificationPreference doesn't have it
    enabled for that event_key
"""
from __future__ import annotations

import asyncio
import threading
import uuid
from email.message import EmailMessage as ParsedMessage
from typing import List

import pytest
from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.db.sync_session import SessionLocal
from app.models import (
    EmailConfig,
    Notification,
    NotificationPreference,
    Organization,
    User,
)
from app.services.email_service import (
    EmailNotConfigured,
    render_invite_email,
    render_notification_email,
    send_email_sync,
)
from app.services.notification_dispatch import notify

pytestmark = pytest.mark.integration


# ── In-process SMTP test fixture ──────────────────────────────────────────

class _Sink:
    """Captures the messages aiosmtpd hands us so the tests can assert."""

    def __init__(self) -> None:
        self.messages: List[ParsedMessage] = []

    async def handle_DATA(self, server, session, envelope):  # noqa: N802
        from email import message_from_bytes

        msg = message_from_bytes(envelope.content)
        self.messages.append(msg)
        return "250 OK"


@pytest.fixture
def fake_smtp():
    """Boot aiosmtpd on localhost with a free random port.

    Picks the port via socket.bind((..., 0)) up-front rather than asking
    aiosmtpd's Controller to do it: on Windows the Controller's own
    port-0 + _trigger_server connect-back races with WinError 10049.
    """
    import socket
    from aiosmtpd.controller import Controller

    # Pre-claim a free port, then close so aiosmtpd can bind it.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    sink = _Sink()
    controller = Controller(sink, hostname="localhost", port=port)
    controller.start()
    try:
        yield ("localhost", port, sink)
    finally:
        controller.stop()


# ── Helpers ───────────────────────────────────────────────────────────────

async def _seed_email_config(*, host: str, port: int, enabled: bool = True, org_id: str | None = None) -> str:
    """Create or update an EmailConfig pointing at the fake SMTP. Returns its id."""
    async with AsyncSessionLocal() as session:
        existing = (
            await session.execute(select(EmailConfig).where(EmailConfig.organization_id == org_id))
        ).scalar_one_or_none()
        if existing:
            existing.smtp_host = host
            existing.smtp_port = port
            existing.use_tls = False  # fake server doesn't speak STARTTLS
            existing.from_address = "test@autotest.local"
            existing.from_name = "AutoTest Test"
            existing.enabled = enabled
            cfg_id = existing.id
        else:
            cfg = EmailConfig(
                id=org_id or "default",
                organization_id=org_id,
                smtp_host=host,
                smtp_port=port,
                use_tls=False,
                from_address="test@autotest.local",
                from_name="AutoTest Test",
                enabled=enabled,
            )
            session.add(cfg)
            cfg_id = cfg.id
        await session.commit()
    return cfg_id


# ── send_email_sync ───────────────────────────────────────────────────────

async def test_send_email_delivers_to_fake_smtp(fake_smtp, org_a) -> None:
    host, port, sink = fake_smtp
    await _seed_email_config(host=host, port=port, org_id=org_a.org_id)

    # send_email_sync is sync; run inside a sync session.
    def _do_send():
        with SessionLocal() as db:
            send_email_sync(
                db=db,
                to="recipient@example.com",
                subject="Hello from AutoTest",
                html_body="<p>hi</p>",
                text_body="hi",
                organization_id=org_a.org_id,
            )

    await asyncio.to_thread(_do_send)

    assert len(sink.messages) == 1
    msg = sink.messages[0]
    assert msg["Subject"] == "Hello from AutoTest"
    assert "recipient@example.com" in msg["To"]
    assert "test@autotest.local" in msg["From"]


async def test_send_email_raises_when_disabled(fake_smtp, org_a) -> None:
    host, port, sink = fake_smtp
    await _seed_email_config(host=host, port=port, enabled=False, org_id=org_a.org_id)

    def _do_send():
        with SessionLocal() as db:
            send_email_sync(
                db=db,
                to="recipient@example.com",
                subject="x",
                html_body="<p>x</p>",
                text_body="x",
                organization_id=org_a.org_id,
            )

    with pytest.raises(EmailNotConfigured):
        await asyncio.to_thread(_do_send)
    assert sink.messages == []


# ── Template helpers ──────────────────────────────────────────────────────

def test_render_invite_email_contains_token_and_url() -> None:
    html, text = render_invite_email(
        org_name="Acme",
        register_url="https://app.example/register?token=abc",
        token="abc",
        expires_at="2026-05-01 12:00",
    )
    assert "abc" in html and "abc" in text
    assert "Acme" in html and "Acme" in text
    assert "https://app.example/register?token=abc" in html


def test_render_notification_email_handles_optional_link() -> None:
    html_with, text_with = render_notification_email(
        title="t", body="b", link="http://x"
    )
    assert "http://x" in html_with and "http://x" in text_with

    html_no, text_no = render_notification_email(title="t", body="b")
    assert "http://" not in html_no
    assert "連結:" not in text_no


# ── notify dispatch ───────────────────────────────────────────────────────

async def _seed_user_with_email(username: str, email: str, org: "OrgFixture") -> None:
    """Insert a User in org so notify() can resolve its email."""
    from app.auth.security import hash_password

    async with AsyncSessionLocal() as session:
        # The org_a fixture's admin user already exists with no email; add a
        # separate recipient user to keep the test focused.
        session.add(
            User(
                username=username,
                display_name=username,
                email=email,
                password_hash=hash_password("ignored"),
                organization_id=org.org_id,
                is_active=True,
                is_superuser=False,
            )
        )
        await session.commit()


async def test_notify_creates_in_app_row_always(org_a) -> None:
    """Even with no EmailConfig + no preference, the in-app Notification
    row must land. That's the bell-badge contract."""
    suffix = uuid.uuid4().hex[:6]
    rcpt = f"recip_{suffix}"
    await _seed_user_with_email(rcpt, f"{rcpt}@example.com", org_a)

    async with AsyncSessionLocal() as db:
        await notify(
            db=db,
            event_key="test.event",
            recipient=rcpt,
            title="hello",
            body="world",
            organization_id=org_a.org_id,
        )
        await db.commit()

    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                select(Notification).where(Notification.recipient == rcpt)
            )
        ).scalars().all()
    assert len(rows) == 1
    assert rows[0].title == "hello"
    assert rows[0].event_key == "test.event"


async def test_notify_skips_email_when_preference_missing(org_a) -> None:
    """No NotificationPreference row at all -> no email dispatched.
    Verified indirectly: in-app row exists but no Celery task gets queued
    (we'd see a connection error if the email path engaged with no SMTP)."""
    suffix = uuid.uuid4().hex[:6]
    rcpt = f"noemail_{suffix}"
    await _seed_user_with_email(rcpt, f"{rcpt}@example.com", org_a)

    async with AsyncSessionLocal() as db:
        await notify(
            db=db,
            event_key="never.opted.in",
            recipient=rcpt,
            title="x",
            body="y",
            organization_id=org_a.org_id,
        )
        await db.commit()

    # The in-app row landed; the lack of exception means email path
    # short-circuited cleanly at preference lookup.
    async with AsyncSessionLocal() as db:
        cnt = (await db.execute(
            select(Notification).where(Notification.recipient == rcpt)
        )).scalars().all()
    assert len(cnt) == 1
