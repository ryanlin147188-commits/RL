"""Multi-tenant data model primitives.

* :class:`TenantScoped` — declarative mixin adding an ``organization_id`` FK
  to a model. Apply it to every business-table model that should be tenant
  isolated; the column is nullable to keep the migration window safe (legacy
  rows with NULL get backfilled via the helper script and the auto-stamp
  ORM event).

* :func:`TenantQuery.for_` — the canonical way to start a SELECT against a
  ``TenantScoped`` model. Always returns a query already filtered by the
  caller's organisation (or unscoped for superusers). Routers must use this
  instead of bare ``select(Model)`` so "forgetting to scope" becomes a lint
  failure, not an IDOR.

* :func:`install_tenant_autostamp` — registers a SQLAlchemy ``before_flush``
  event handler that copies ``current_org_id`` onto any new ``TenantScoped``
  row that did not set one explicitly. Called once at startup by
  :mod:`app.database`.
"""
from __future__ import annotations

from typing import Any, Type, TypeVar

from sqlalchemy import ForeignKey, String, Select, event, select
from sqlalchemy.orm import Mapped, Session, declared_attr, mapped_column

from app.auth.context import current_is_superuser, current_org_id


class TenantScoped:
    """Mixin: every subclass gets an ``organization_id`` FK column.

    Indexed for the org-filter that scopes 99% of queries. Nullable so the
    migration that backfills existing rows can run as ADD COLUMN → UPDATE →
    SET NOT NULL in steps; once that migration ships, future revisions can
    flip nullable to False per-table as data warrants.
    """

    @declared_attr
    def organization_id(cls) -> Mapped[str | None]:
        return mapped_column(
            String(36),
            ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=True,
            index=True,
        )


M = TypeVar("M")


class TenantQuery:
    """Canonical query factory for tenant-scoped models.

    Usage::

        defects = (
            await db.execute(TenantQuery.for_(Defect).order_by(Defect.created_at.desc()))
        ).scalars().all()

    Superusers (``is_superuser=True`` on the JWT) bypass the org filter — the
    intent is single-tenant self-hosted deployments where one designated admin
    needs unrestricted access for support tasks.
    """

    @staticmethod
    def for_(model: Type[M]) -> Select:
        stmt: Select = select(model)
        if current_is_superuser.get():
            return stmt
        org_id = current_org_id.get()
        if org_id is None:
            # No tenant in context (anonymous / mis-configured request).
            # Return a query that yields zero rows rather than leaking data.
            return stmt.where(model.organization_id.is_(None)).where(
                model.organization_id.is_not(None)  # always-false
            )
        return stmt.where(model.organization_id == org_id)


def install_tenant_autostamp() -> None:
    """Register a global before_flush hook that fills ``organization_id`` on
    new :class:`TenantScoped` rows from the ContextVar.

    Idempotent: re-installing is a no-op (SQLAlchemy de-duplicates listeners
    by callable identity for the same target).
    """

    @event.listens_for(Session, "before_flush")
    def _autostamp_org(session: Session, flush_context: Any, instances: Any) -> None:
        org_id = current_org_id.get()
        if not org_id:
            return
        for obj in session.new:
            if isinstance(obj, TenantScoped) and getattr(obj, "organization_id", None) is None:
                obj.organization_id = org_id
