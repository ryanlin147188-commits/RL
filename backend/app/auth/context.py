"""Per-request authentication context propagated via :class:`ContextVar`.

The middleware sets these once per request after decoding the JWT; ORM event
hooks read them to auto-stamp ``organization_id`` on new rows, and helpers
read them to scope queries.

ContextVar (rather than a thread-local) is the right primitive because
FastAPI / asyncio runs each request inside its own task, and each task
inherits the parent's context but its mutations stay isolated.

Background tasks (Celery) get a sync equivalent via ``set_org_context``
called at task entry — see ``backend/app/tasks/_base.py`` (RFC-9).
"""
from __future__ import annotations

from contextvars import ContextVar, Token
from typing import Optional

current_org_id: ContextVar[Optional[str]] = ContextVar("current_org_id", default=None)
current_username: ContextVar[Optional[str]] = ContextVar("current_username", default=None)
current_is_superuser: ContextVar[bool] = ContextVar("current_is_superuser", default=False)


class _Snapshot:
    """Tokens returned by :func:`set_request_context` so a caller can restore
    the prior values exactly. Used by the auth middleware in ``finally`` and by
    Celery task wrappers."""

    __slots__ = ("org", "user", "su")

    def __init__(self, org: Token, user: Token, su: Token) -> None:
        self.org = org
        self.user = user
        self.su = su


def set_request_context(
    *,
    org_id: Optional[str],
    username: Optional[str],
    is_superuser: bool,
) -> _Snapshot:
    return _Snapshot(
        org=current_org_id.set(org_id),
        user=current_username.set(username),
        su=current_is_superuser.set(is_superuser),
    )


def reset_request_context(snap: _Snapshot) -> None:
    current_org_id.reset(snap.org)
    current_username.reset(snap.user)
    current_is_superuser.reset(snap.su)
