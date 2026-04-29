"""POST /api/auth/bootstrap-invite — first-admin bootstrap (double-gated)."""
from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import select

from app.auth.security import hash_password
from app.database import AsyncSessionLocal
from app.models import Organization, Role, User

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _bootstrap_token(monkeypatch):
    """Default: enable the endpoint with a known test token.
    Tests that need the env unset re-set it within the test body."""
    monkeypatch.setenv("AUTOTEST_BOOTSTRAP_TOKEN", "test-bootstrap-token-12345")
    yield


async def test_bootstrap_invite_happy_path(client) -> None:
    """Token matches + no admin exists -> 201 with usable invite."""
    resp = await client.post(
        "/api/auth/bootstrap-invite",
        json={
            "bootstrap_token": "test-bootstrap-token-12345",
            "organization_slug": "default",
            "ttl_hours": 1,
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["invite_token"].startswith("BOOT-")
    assert body["organization_slug"] == "default"
    assert body["role"] == "Admin"

    # The token actually works for /register.
    username = f"first_admin_{uuid.uuid4().hex[:6]}"
    reg = await client.post(
        "/api/auth/register",
        json={
            "username": username,
            "password": "first-admin-password",
            "email": f"{username}@example.com",
            "invite_token": body["invite_token"],
        },
    )
    assert reg.status_code == 201, reg.text


async def test_bootstrap_invite_disabled_when_env_unset(client, monkeypatch) -> None:
    """No AUTOTEST_BOOTSTRAP_TOKEN -> endpoint is disabled (503)."""
    monkeypatch.delenv("AUTOTEST_BOOTSTRAP_TOKEN", raising=False)
    resp = await client.post(
        "/api/auth/bootstrap-invite",
        json={"bootstrap_token": "anything", "organization_slug": "default"},
    )
    assert resp.status_code == 503
    assert "disabled" in resp.json()["detail"].lower()


async def test_bootstrap_invite_rejects_wrong_token(client) -> None:
    resp = await client.post(
        "/api/auth/bootstrap-invite",
        json={"bootstrap_token": "wrong-token", "organization_slug": "default"},
    )
    assert resp.status_code == 403
    assert resp.json()["detail"] == "bootstrap_token mismatch"


async def test_bootstrap_invite_blocked_when_admin_exists(client) -> None:
    """Once any active admin (superuser or Admin role) is in the org, the
    endpoint refuses — preventing accidental re-bootstrap."""
    # Seed an active admin in the default org.
    async with AsyncSessionLocal() as session:
        org = (
            await session.execute(select(Organization).where(Organization.slug == "default"))
        ).scalar_one()
        admin_role = (
            await session.execute(select(Role).where(Role.name == "Admin"))
        ).scalar_one()
        session.add(
            User(
                username=f"existing-admin-{uuid.uuid4().hex[:6]}",
                display_name="existing",
                email="ea@example.com",
                password_hash=hash_password("existing-admin-pwd"),
                role_id=admin_role.id,
                organization_id=org.id,
                is_superuser=False,
                is_active=True,
            )
        )
        await session.commit()

    resp = await client.post(
        "/api/auth/bootstrap-invite",
        json={"bootstrap_token": "test-bootstrap-token-12345", "organization_slug": "default"},
    )
    assert resp.status_code == 409
    assert "already has" in resp.json()["detail"]


async def test_bootstrap_invite_blocked_by_superuser(client) -> None:
    """is_superuser=True (without role) also counts as an admin for the gate."""
    async with AsyncSessionLocal() as session:
        org = (
            await session.execute(select(Organization).where(Organization.slug == "default"))
        ).scalar_one()
        session.add(
            User(
                username=f"super-{uuid.uuid4().hex[:6]}",
                display_name="su",
                email="su@example.com",
                password_hash=hash_password("super-pwd"),
                role_id=None,
                organization_id=org.id,
                is_superuser=True,
                is_active=True,
            )
        )
        await session.commit()

    resp = await client.post(
        "/api/auth/bootstrap-invite",
        json={"bootstrap_token": "test-bootstrap-token-12345", "organization_slug": "default"},
    )
    assert resp.status_code == 409


async def test_bootstrap_invite_inactive_admin_does_not_block(client) -> None:
    """An is_active=False admin should NOT count — the org effectively has
    no admin, so bootstrap should succeed."""
    async with AsyncSessionLocal() as session:
        org = (
            await session.execute(select(Organization).where(Organization.slug == "default"))
        ).scalar_one()
        admin_role = (
            await session.execute(select(Role).where(Role.name == "Admin"))
        ).scalar_one()
        session.add(
            User(
                username=f"deactivated-{uuid.uuid4().hex[:6]}",
                display_name="x",
                email="x@example.com",
                password_hash=hash_password("dont-care"),
                role_id=admin_role.id,
                organization_id=org.id,
                is_superuser=False,
                is_active=False,   # deactivated
            )
        )
        await session.commit()

    resp = await client.post(
        "/api/auth/bootstrap-invite",
        json={"bootstrap_token": "test-bootstrap-token-12345", "organization_slug": "default"},
    )
    assert resp.status_code == 201


async def test_bootstrap_invite_unknown_org(client) -> None:
    resp = await client.post(
        "/api/auth/bootstrap-invite",
        json={
            "bootstrap_token": "test-bootstrap-token-12345",
            "organization_slug": "no-such-org",
        },
    )
    assert resp.status_code == 404
