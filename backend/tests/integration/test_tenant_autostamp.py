"""ORM ``before_flush`` listener stamps ``organization_id`` on new
:class:`TenantScoped` rows from the request context.

Together with :func:`TenantQuery.for_`, this is the half of RFC-4 that makes
"forgetting to scope" a write a non-issue: even if a router never sets
``organization_id`` explicitly, the row inherits it from the JWT-derived
ContextVar set by :class:`AuthMiddleware`.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.auth.tenant import TenantQuery
from app.database import AsyncSessionLocal
from app.models import Defect

pytestmark = pytest.mark.integration


async def test_create_defect_via_api_inherits_org_id(client, org_a) -> None:
    """An end-to-end POST that does not pass org_id in the body still ends up
    with the caller's org_id stamped by the ORM event listener."""
    payload = {
        "project_id": org_a.project_id,
        "title": "auto-stamp probe",
        "description": "the listener should fill org_id from the JWT context",
    }
    resp = await client.post("/api/defects", json=payload, headers=org_a.headers)
    assert resp.status_code == 201, resp.text

    async with AsyncSessionLocal() as session:
        rows = (await session.execute(select(Defect))).scalars().all()
    assert len(rows) == 1
    assert rows[0].organization_id == org_a.org_id, (
        f"expected org_id={org_a.org_id}, got {rows[0].organization_id}"
    )


async def test_tenant_query_filters_to_caller_org(client, org_a, org_b) -> None:
    """Use TenantQuery.for_(Defect) directly inside an async session — proves
    the query produces only rows belonging to the active ContextVar org."""
    # Create defects in both tenants via the API (auto-stamps each row).
    for org in (org_a, org_b):
        await client.post(
            "/api/defects",
            json={"project_id": org.project_id, "title": f"defect-in-{org.org_id[:6]}"},
            headers=org.headers,
        )

    # Now drive a TenantQuery directly with org_a's context.
    from app.auth.context import current_is_superuser, current_org_id

    org_token = current_org_id.set(org_a.org_id)
    su_token = current_is_superuser.set(False)
    try:
        async with AsyncSessionLocal() as session:
            rows = (await session.execute(TenantQuery.for_(Defect))).scalars().all()
    finally:
        current_org_id.reset(org_token)
        current_is_superuser.reset(su_token)

    assert len(rows) == 1
    assert rows[0].organization_id == org_a.org_id
