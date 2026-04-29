"""RFC-6 token revocation: ``POST /api/auth/logout`` blocklists the caller's
access token via Valkey, and :class:`AuthMiddleware` rejects the same token
on subsequent requests.
"""
from __future__ import annotations

import uuid

import pytest

from app.auth.security import hash_password
from app.database import AsyncSessionLocal
from app.models import Organization, Role, User


pytestmark = pytest.mark.integration


async def _login(client, *, username: str, password: str) -> str:
    """Seed a user, then drive the login endpoint and return its access token."""
    from sqlalchemy import select

    async with AsyncSessionLocal() as session:
        org = (
            await session.execute(select(Organization).where(Organization.slug == "default"))
        ).scalar_one()
        admin_role = (
            await session.execute(select(Role).where(Role.name == "Admin"))
        ).scalar_one_or_none()
        session.add(
            User(
                username=username,
                display_name=username,
                email=f"{username}@test.local",
                password_hash=hash_password(password),
                role_id=admin_role.id if admin_role else None,
                organization_id=org.id,
                is_superuser=True,  # bypasses RBAC for these tests
                is_active=True,
            )
        )
        await session.commit()

    resp = await client.post(
        "/api/auth/login",
        json={"username": username, "password": password},
    )
    assert resp.status_code == 200
    return resp.json()["access_token"]


async def test_logout_blocks_subsequent_requests(client) -> None:
    """The classic flow: get a token, do work, log out, replay -> 401."""
    username = f"logout-{uuid.uuid4().hex[:6]}"
    token = await _login(client, username=username, password="correct-horse-battery")
    headers = {"Authorization": f"Bearer {token}"}

    # Pre-logout: the token works.
    me = await client.get("/api/auth/me", headers=headers)
    assert me.status_code == 200

    # Log out.
    out = await client.post("/api/auth/logout", headers=headers)
    assert out.status_code == 204

    # Post-logout: the same token is now rejected.
    replay = await client.get("/api/auth/me", headers=headers)
    assert replay.status_code == 401
    assert "撤銷" in replay.json().get("detail", "") or "revoked" in replay.text.lower()


async def test_logout_is_idempotent(client) -> None:
    username = f"idem-{uuid.uuid4().hex[:6]}"
    token = await _login(client, username=username, password="correct-horse-battery")
    headers = {"Authorization": f"Bearer {token}"}

    first = await client.post("/api/auth/logout", headers=headers)
    assert first.status_code == 204

    # Second logout uses an already-revoked token; middleware rejects before
    # the handler runs. 401 (revoked) -- not a 500.
    second = await client.post("/api/auth/logout", headers=headers)
    assert second.status_code == 401


async def test_refresh_still_works_after_access_token_logout(client) -> None:
    """Logging out only revokes the access token; the refresh token's separate
    jti is untouched and can mint a new access token."""
    username = f"refresh-after-logout-{uuid.uuid4().hex[:6]}"
    # Use the higher-level login endpoint so we keep the matching refresh token.
    from sqlalchemy import select

    async with AsyncSessionLocal() as session:
        org = (
            await session.execute(select(Organization).where(Organization.slug == "default"))
        ).scalar_one()
        admin_role = (
            await session.execute(select(Role).where(Role.name == "Admin"))
        ).scalar_one_or_none()
        session.add(
            User(
                username=username,
                display_name=username,
                email=f"{username}@test.local",
                password_hash=hash_password("correct-horse-battery"),
                role_id=admin_role.id if admin_role else None,
                organization_id=org.id,
                is_superuser=True,
                is_active=True,
            )
        )
        await session.commit()

    login = await client.post(
        "/api/auth/login",
        json={"username": username, "password": "correct-horse-battery"},
    )
    body = login.json()
    access = body["access_token"]
    refresh = body["refresh_token"]

    # Log out the access token.
    await client.post(
        "/api/auth/logout", headers={"Authorization": f"Bearer {access}"}
    )

    # Refresh token should still mint a new access token.
    new = await client.post("/api/auth/refresh", json={"refresh_token": refresh})
    assert new.status_code == 200
    new_access = new.json()["access_token"]
    assert new_access != access

    # The freshly-minted access token works.
    me = await client.get(
        "/api/auth/me", headers={"Authorization": f"Bearer {new_access}"}
    )
    assert me.status_code == 200


async def test_unauthenticated_logout_returns_401(client) -> None:
    """No bearer header — middleware rejects before reaching the handler."""
    resp = await client.post("/api/auth/logout")
    assert resp.status_code == 401
