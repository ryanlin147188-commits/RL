"""Pure-unit checks on :func:`TenantQuery.for_`.

These exercise the SQL fragment that comes out of the helper without hitting
a database. Stronger end-to-end IDOR coverage lives in
``tests/integration/test_idor.py``.
"""
from __future__ import annotations

import pytest
from sqlalchemy.dialects import postgresql

from app.auth.context import current_is_superuser, current_org_id
from app.auth.tenant import TenantQuery
from app.models.defect import Defect


def _compile(stmt) -> str:
    return str(stmt.compile(
        dialect=postgresql.dialect(),
        compile_kwargs={"literal_binds": True},
    ))


def test_for_returns_org_filtered_query() -> None:
    org_token = current_org_id.set("org-AAA")
    su_token = current_is_superuser.set(False)
    try:
        sql = _compile(TenantQuery.for_(Defect))
        assert "defects.organization_id = 'org-AAA'" in sql
    finally:
        current_org_id.reset(org_token)
        current_is_superuser.reset(su_token)


def test_for_superuser_skips_filter() -> None:
    su_token = current_is_superuser.set(True)
    org_token = current_org_id.set("org-AAA")
    try:
        sql = _compile(TenantQuery.for_(Defect))
        # Superuser path: no WHERE on organization_id
        assert "organization_id" not in sql or "WHERE" not in sql.upper()
    finally:
        current_is_superuser.reset(su_token)
        current_org_id.reset(org_token)


def test_for_no_context_returns_zero_row_query() -> None:
    """Anonymous / mis-configured request — must not leak rows even if a route
    accidentally reaches an ORM query without a tenant context."""
    org_token = current_org_id.set(None)
    su_token = current_is_superuser.set(False)
    try:
        sql = _compile(TenantQuery.for_(Defect))
        # The helper emits an always-false predicate. Either column IS NULL
        # AND IS NOT NULL, or whatever logically-empty form SQLAlchemy compiles.
        assert "IS NULL" in sql.upper() and "IS NOT NULL" in sql.upper()
    finally:
        current_org_id.reset(org_token)
        current_is_superuser.reset(su_token)
