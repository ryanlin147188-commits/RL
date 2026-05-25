"""SMTP email sender (sync, called from Celery worker).

Reads :class:`EmailConfig` for the caller's organisation, opens a one-shot
SMTP connection, and sends a multipart text/HTML message. Pure stdlib so
the Celery image (which can't take aiosmtplib because of the protobuf pin
chain) builds without churn.

Two custom exceptions:
    * :class:`EmailNotConfigured` — DB has no row, or row has ``enabled=False``,
      or the host/from-address are blank. Caller should treat this as a
      no-op (notifications still get the in-app row).
    * :class:`EmailSendFailed` — SMTP transport error after a config was
      found. Caller may retry.

Usage from a Celery task::

    from app.services.email_service import send_email_sync
    send_email_sync(
        db=session,
        to="user@example.com",
        subject="您有一張新的審核單",
        html_body=invite_html,
        text_body=invite_text,
        organization_id=org_id,
    )
"""
from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage
from email.utils import formataddr
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.email_config import EmailConfig

log = logging.getLogger(__name__)


class EmailNotConfigured(RuntimeError):
    """No usable EmailConfig for this org (or it's disabled)."""


class EmailSendFailed(RuntimeError):
    """SMTP server rejected the message after we connected."""


def _resolve_config(db: Session, organization_id: Optional[str]) -> EmailConfig:
    """Find the EmailConfig for ``organization_id``, falling back to the
    org-less ``id='default'`` row. Raises EmailNotConfigured if neither is
    usable."""
    stmt = select(EmailConfig).where(EmailConfig.organization_id == organization_id)
    cfg = db.execute(stmt).scalar_one_or_none()
    if cfg is None:
        # Per-org row absent — try the global default
        cfg = db.get(EmailConfig, "default")
    if cfg is None or not cfg.enabled:
        raise EmailNotConfigured(
            f"no enabled EmailConfig for org_id={organization_id!r}"
        )
    if not (cfg.smtp_host and cfg.from_address):
        raise EmailNotConfigured(
            f"EmailConfig for org_id={organization_id!r} is missing host or from_address"
        )
    return cfg


def _send_with_config(
    cfg: EmailConfig,
    *,
    to: str,
    subject: str,
    html_body: str,
    text_body: str,
) -> None:
    """Low-level send using a pre-loaded EmailConfig. Raises EmailSendFailed on SMTP error.
    Called by both send_email_sync (Celery path) and the async FastAPI test endpoint."""
    if not to or "@" not in to:
        raise ValueError(f"invalid recipient: {to!r}")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((cfg.from_name or "AutoTest", cfg.from_address))
    msg["To"] = to
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")

    host = cfg.smtp_host
    port = int(cfg.smtp_port or 587)

    try:
        with smtplib.SMTP(host, port, timeout=30) as server:
            server.ehlo()
            if cfg.use_tls:
                server.starttls()
                server.ehlo()
            if cfg.smtp_user and cfg.smtp_password:
                server.login(cfg.smtp_user, cfg.smtp_password)
            server.send_message(msg)
        log.info("email sent: to=%s subject=%r", to, subject)
    except smtplib.SMTPException as exc:
        log.warning("SMTP send failed: to=%s err=%s", to, exc)
        raise EmailSendFailed(str(exc)) from exc
    except OSError as exc:
        log.warning("SMTP transport error: to=%s err=%s", to, exc)
        raise EmailSendFailed(str(exc)) from exc


def send_email_sync(
    *,
    db: Session,
    to: str,
    subject: str,
    html_body: str,
    text_body: str,
    organization_id: Optional[str] = None,
) -> None:
    """Send one email synchronously. Designed to be called inside a
    Celery worker (which is sync); FastAPI handlers should go through
    :func:`app.services.notification_dispatch.notify` which enqueues the
    Celery task instead of blocking the request thread.
    """
    cfg = _resolve_config(db, organization_id)
    _send_with_config(cfg, to=to, subject=subject, html_body=html_body, text_body=text_body)


# ── Email body templates ──────────────────────────────────────────────────
# Inline f-string templates so Phase 1 has no extra file or library
# dependency. When the Phase grows (more locales, attachments) move to
# a templates/ directory rendered by jinja2.

_INVITE_HTML_ZH = """\
<html><body style="font-family:system-ui,-apple-system,sans-serif;color:#1f2937;max-width:600px">
<h2 style="color:#7c3aed">AutoTest 邀請註冊</h2>
<p>您好,</p>
<p>您被邀請加入組織 <b>{org_name}</b>。請點擊下方連結完成註冊:</p>
<p><a href="{register_url}" style="display:inline-block;padding:10px 18px;background:#7c3aed;color:white;text-decoration:none;border-radius:6px">點此完成註冊</a></p>
<p>若連結失效,可手動到登入頁,於註冊表單填入下列邀請碼:</p>
<pre style="background:#f3f4f6;padding:10px;border-radius:6px;font-size:13px">{token}</pre>
<p style="color:#6b7280;font-size:12px">此邀請碼將於 {expires_at} 過期,且僅可使用一次。</p>
<hr style="border:none;border-top:1px solid #e5e7eb;margin-top:24px">
<p style="color:#9ca3af;font-size:11px">本信件由 AutoTest 自動發出,請勿直接回覆。</p>
</body></html>
"""

_INVITE_TEXT_ZH = """\
AutoTest 邀請註冊

您好,

您被邀請加入組織 {org_name}。

註冊連結:
{register_url}

或手動於登入頁填入邀請碼: {token}

此邀請碼將於 {expires_at} 過期,且僅可使用一次。
"""

_NOTIFY_HTML_ZH = """\
<html><body style="font-family:system-ui,-apple-system,sans-serif;color:#1f2937;max-width:600px">
<h3 style="color:#7c3aed;margin:0 0 12px">{title}</h3>
<div style="white-space:pre-wrap;color:#374151">{body}</div>
{link_html}
<hr style="border:none;border-top:1px solid #e5e7eb;margin-top:24px">
<p style="color:#9ca3af;font-size:11px">本信件由 AutoTest 自動發出。如不再想接收此類通知,請至「設定 → 通知」修改偏好。</p>
</body></html>
"""

_NOTIFY_TEXT_ZH = """\
{title}

{body}

{link_text}

---
本信件由 AutoTest 自動發出。如不再想接收此類通知,請至「設定 → 通知」修改偏好。
"""


def render_invite_email(
    *,
    org_name: str,
    register_url: str,
    token: str,
    expires_at: str,
) -> tuple[str, str]:
    """Return (html_body, text_body) for the invite email."""
    fmt = {"org_name": org_name, "register_url": register_url, "token": token, "expires_at": expires_at}
    return _INVITE_HTML_ZH.format(**fmt), _INVITE_TEXT_ZH.format(**fmt)


_PWD_RESET_HTML_ZH = """\
<html><body style="font-family:system-ui,-apple-system,sans-serif;color:#1f2937;max-width:600px">
<h2 style="color:#d97706">AutoTest 密碼重置</h2>
<p>您好 <b>{display_name}</b>,</p>
<p>系統收到您的密碼重置請求。請點擊下方連結設定新密碼:</p>
<p><a href="{reset_url}" style="display:inline-block;padding:10px 18px;background:#d97706;color:white;text-decoration:none;border-radius:6px">設定新密碼 →</a></p>
<p style="color:#6b7280;font-size:12px">連結將於 <b>{expires_at}</b> 過期,且僅可使用一次。</p>
<p style="color:#6b7280;font-size:12px">若連結無法點擊,請複製以下網址至瀏覽器:</p>
<pre style="background:#f3f4f6;padding:10px;border-radius:6px;font-size:12px;word-break:break-all;white-space:pre-wrap">{reset_url}</pre>
<hr style="border:none;border-top:1px solid #e5e7eb;margin-top:24px">
<p style="color:#9ca3af;font-size:11px">若您沒有發起此請求,請忽略本信。本信件由 AutoTest 自動發出,請勿直接回覆。</p>
</body></html>
"""

_PWD_RESET_TEXT_ZH = """\
AutoTest 密碼重置

您好 {display_name},

系統收到您的密碼重置請求。請點擊下方連結設定新密碼:

{reset_url}

連結將於 {expires_at} 過期,且僅可使用一次。

若您沒有發起此請求,請忽略本信。
"""


def render_password_reset_email(
    *,
    display_name: str,
    reset_url: str,
    expires_at: str,
) -> tuple[str, str]:
    """Return (html_body, text_body) for the forgot-password reset link email."""
    fmt = {"display_name": display_name, "reset_url": reset_url, "expires_at": expires_at}
    return _PWD_RESET_HTML_ZH.format(**fmt), _PWD_RESET_TEXT_ZH.format(**fmt)


# ── v1.1.10 新增:自助註冊驗證信 ────────────────────────────
_REG_VERIFY_HTML_ZH = """\
<html><body style="font-family:system-ui,-apple-system,sans-serif;color:#1f2937;max-width:600px">
<h2 style="color:#d97706">AutoTest 帳號啟用驗證</h2>
<p>您好 <b>{display_name}</b>,</p>
<p>感謝您註冊 AutoTest。請點擊下方連結啟用帳號:</p>
<p><a href="{verify_url}" style="display:inline-block;padding:10px 18px;background:#d97706;color:white;text-decoration:none;border-radius:6px">啟用帳號 →</a></p>
<p style="color:#6b7280;font-size:12px">連結將於 <b>{expires_at}</b> 過期,且僅可使用一次。</p>
<p style="color:#6b7280;font-size:12px">啟用後即可登入,但要等管理員指派專案 / 角色才能完整使用平台功能。</p>
<p style="color:#6b7280;font-size:12px">若連結無法點擊,請複製以下網址至瀏覽器:</p>
<pre style="background:#f3f4f6;padding:10px;border-radius:6px;font-size:12px;word-break:break-all;white-space:pre-wrap">{verify_url}</pre>
<hr style="border:none;border-top:1px solid #e5e7eb;margin-top:24px">
<p style="color:#9ca3af;font-size:11px">若您沒有註冊本帳號,請忽略本信。本信件由 AutoTest 自動發出,請勿直接回覆。</p>
</body></html>
"""

_REG_VERIFY_TEXT_ZH = """\
AutoTest 帳號啟用驗證

您好 {display_name},

感謝您註冊 AutoTest。請點擊下方連結啟用帳號:

{verify_url}

連結將於 {expires_at} 過期,且僅可使用一次。
啟用後即可登入,但要等管理員指派專案 / 角色才能完整使用平台功能。

若您沒有註冊本帳號,請忽略本信。
"""


def render_registration_verify_email(
    *,
    display_name: str,
    verify_url: str,
    expires_at: str,
) -> tuple[str, str]:
    """Return (html_body, text_body) for the self-registration verify link email."""
    fmt = {"display_name": display_name, "verify_url": verify_url, "expires_at": expires_at}
    return _REG_VERIFY_HTML_ZH.format(**fmt), _REG_VERIFY_TEXT_ZH.format(**fmt)


def render_notification_email(
    *,
    title: str,
    body: str,
    link: Optional[str] = None,
) -> tuple[str, str]:
    """Return (html_body, text_body) for a generic notification email."""
    if link:
        link_html = (
            f'<p><a href="{link}" style="color:#7c3aed">點此查看 →</a></p>'
        )
        link_text = f"連結: {link}"
    else:
        link_html = ""
        link_text = ""
    fmt = {"title": title, "body": body, "link_html": link_html, "link_text": link_text}
    return _NOTIFY_HTML_ZH.format(**fmt), _NOTIFY_TEXT_ZH.format(**fmt)
