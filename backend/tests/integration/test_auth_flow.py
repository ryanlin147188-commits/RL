"""End-to-end auth flow: login → access protected → refresh."""
from __future__ import annotations

import uuid

import pytest

from app.auth.security import hash_password
from app.database import AsyncSessionLocal
from app.models import Organization, Role, User


pytestmark = pytest.mark.integration


async def _seed_user(*, username: str, password: str) -> str:
    """Insert one User attached to the default org. Returns the org id."""
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
                is_superuser=False,
                is_active=True,
            )
        )
        await session.commit()
        return org.id


async def test_login_returns_access_and_refresh(client) -> None:
    username = f"login-{uuid.uuid4().hex[:6]}"
    await _seed_user(username=username, password="correct-horse-battery")

    resp = await client.post(
        "/api/auth/login",
        json={"username": username, "password": "correct-horse-battery"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["access_token"]
    assert body["refresh_token"]
    assert body["expires_in"] > 0


async def test_login_rejects_wrong_password(client) -> None:
    username = f"wrong-{uuid.uuid4().hex[:6]}"
    await _seed_user(username=username, password="correct-horse-battery")

    resp = await client.post(
        "/api/auth/login",
        json={"username": username, "password": "guess-guess-guess"},
    )
    assert resp.status_code == 401


async def test_me_requires_bearer(client) -> None:
    resp = await client.get("/api/auth/me")
    assert resp.status_code == 401


async def test_me_with_valid_bearer_returns_user(client) -> None:
    username = f"me-{uuid.uuid4().hex[:6]}"
    await _seed_user(username=username, password="correct-horse-battery")
    login = await client.post(
        "/api/auth/login",
        json={"username": username, "password": "correct-horse-battery"},
    )
    token = login.json()["access_token"]

    resp = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json()["username"] == username


async def test_refresh_issues_new_pair(client) -> None:
    username = f"refresh-{uuid.uuid4().hex[:6]}"
    await _seed_user(username=username, password="correct-horse-battery")
    login = await client.post(
        "/api/auth/login",
        json={"username": username, "password": "correct-horse-battery"},
    )
    refresh = login.json()["refresh_token"]

    resp = await client.post("/api/auth/refresh", json={"refresh_token": refresh})
    assert resp.status_code == 200
    new_pair = resp.json()
    assert new_pair["access_token"]
    # Token decode confirms it is a valid access token for the same user.
    # Byte-equality with the original is not asserted: JWT iat/exp are seconds,
    # so a same-second refresh deterministically produces an identical token.
    from app.auth.security import decode_token
    payload = decode_token(new_pair["access_token"])
    assert payload["sub"] == username
    assert payload["typ"] == "access"


async def test_refresh_rejects_access_token_as_refresh(client) -> None:
    """Sending an access token to /auth/refresh must be rejected — `typ` check."""
    username = f"misuse-{uuid.uuid4().hex[:6]}"
    await _seed_user(username=username, password="correct-horse-battery")
    login = await client.post(
        "/api/auth/login",
        json={"username": username, "password": "correct-horse-battery"},
    )
    access = login.json()["access_token"]

    resp = await client.post("/api/auth/refresh", json={"refresh_token": access})
    assert resp.status_code == 401
