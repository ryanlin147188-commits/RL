"""Anonymous self-service invite endpoints (Phase 4).

Covers:
  * GET  /api/organizations/by-email-domain  -- domain → org lookup
  * POST /api/auth/request-access            -- mint invite + email it

Both must be reachable without an Authorization header (the middleware
whitelist applies). The token itself must NEVER appear in the HTTP
response — only in the OrgInvite row (and, in production, the email).
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models.org_invite import OrgInvite
from app.models.organization import Organization

pytestmark = pytest.mark.integration


async def _set_email_domain(org_id: str, domain: str) -> None:
    async with AsyncSessionLocal() as db:
        org = await db.get(Organization, org_id)
        org.email_domains = domain
        await db.commit()


# ── /by-email-domain ───────────────────────────────────────────────────

async def test_by_email_domain_returns_org_for_known_domain(client, org_a) -> None:
    domain = f"acme-{uuid.uuid4().hex[:6]}.test"
    await _set_email_domain(org_a.org_id, domain)

    # Anonymous: no Authorization header
    resp = await client.get(
        f"/api/organizations/by-email-domain?email=alice@{domain}"
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["organization_id"] == org_a.org_id


async def test_by_email_domain_404_for_unknown_domain(client) -> None:
    resp = await client.get(
        "/api/organizations/by-email-domain?email=nobody@unknown.xyz"
    )
    assert resp.status_code == 404


async def test_by_email_domain_400_for_bad_email(client) -> None:
    resp = await client.get(
        "/api/organizations/by-email-domain?email=not-an-email"
    )
    assert resp.status_code == 400


# ── /auth/request-access ───────────────────────────────────────────────

async def test_request_access_creates_invite_for_known_domain(client, org_a) -> None:
    domain = f"acme-{uuid.uuid4().hex[:6]}.test"
    await _set_email_domain(org_a.org_id, domain)
    email = f"alice-{uuid.uuid4().hex[:6]}@{domain}"

    resp = await client.post(
        "/api/auth/request-access",
        json={"email": email, "display_name": "Alice"},
    )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["sent"] is True
    assert body["organization_slug"] == (
        await _get_org_slug(org_a.org_id)
    )
    # Email must be masked in response (not the raw value)
    assert email not in body["masked_email"]
    assert body["masked_email"].endswith(f"@{domain}")

    # Invite row landed
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(OrgInvite).where(OrgInvite.email == email)
        )).scalars().all()
    assert len(rows) == 1
    inv = rows[0]
    assert inv.organization_id == org_a.org_id
    assert inv.token.startswith("REQ-")
    assert inv.email_sent_at is not None
    assert inv.email_sent_to == email
    # Token must NEVER appear in the HTTP response (delivered via email only)
    assert inv.token not in resp.text


async def test_request_access_rejects_unknown_domain(client) -> None:
    resp = await client.post(
        "/api/auth/request-access",
        json={"email": f"nobody-{uuid.uuid4().hex[:6]}@unknown.xyz"},
    )
    assert resp.status_code == 400
    body = resp.json()
    detail = body["detail"]
    assert isinstance(detail, dict)
    assert detail["error"] == "unknown_domain"


async def test_request_access_rejects_invalid_email(client) -> None:
    resp = await client.post(
        "/api/auth/request-access",
        json={"email": "not-an-email"},
    )
    assert resp.status_code == 400


async def test_request_access_60s_cooldown(client, org_a) -> None:
    """Same email twice within 60 seconds → 429 (don't spam users)."""
    domain = f"acme-{uuid.uuid4().hex[:6]}.test"
    await _set_email_domain(org_a.org_id, domain)
    email = f"alice-{uuid.uuid4().hex[:6]}@{domain}"

    first = await client.post(
        "/api/auth/request-access", json={"email": email}
    )
    assert first.status_code == 202

    second = await client.post(
        "/api/auth/request-access", json={"email": email}
    )
    assert second.status_code == 429


# ── Cross-org domain uniqueness (Phase 4D) ──────────────────────────────

async def test_email_domain_cross_org_unique_enforced(client, org_a, org_b) -> None:
    """Two orgs cannot both claim the same email_domains entry."""
    from app.auth.security import create_access_token
    from app.models.user import User
    domain = f"shared-{uuid.uuid4().hex[:6]}.test"

    # The PUT /organizations endpoint is superuser-only and reads is_superuser
    # off the DB row (not JWT claim). Promote both org admins so we can drive
    # the cross-org write paths.
    async with AsyncSessionLocal() as db:
        for uname in (org_a.username, org_b.username):
            u = (
                await db.execute(select(User).where(User.username == uname))
            ).scalar_one()
            u.is_superuser = True
        await db.commit()

    su_a_token = create_access_token(
        org_a.username, extra={"org_id": org_a.org_id, "is_superuser": True}
    )
    su_b_token = create_access_token(
        org_b.username, extra={"org_id": org_b.org_id, "is_superuser": True}
    )

    # Org A claims domain — succeeds
    r1 = await client.put(
        f"/api/organizations/{org_a.org_id}",
        json={"email_domains": domain},
        headers={"Authorization": f"Bearer {su_a_token}"},
    )
    assert r1.status_code == 200, r1.text

    # Org B tries to claim the same domain — 409
    r2 = await client.put(
        f"/api/organizations/{org_b.org_id}",
        json={"email_domains": domain},
        headers={"Authorization": f"Bearer {su_b_token}"},
    )
    assert r2.status_code == 409
    body = r2.json()
    assert isinstance(body["detail"], dict)
    assert body["detail"]["error"] == "domain_conflict"
    # The conflicts list should mention the actual conflicting org slug
    conflicts = body["detail"]["conflicts"]
    assert any(c["domain"] == domain for c in conflicts)


# ── Redeem invite (post-login org switch) ───────────────────────────────

async def test_redeem_invite_switches_org_and_returns_new_tokens(client, org_a, org_b) -> None:
    """A user logged into org A can paste an invite for org B and end up in org B
    after redeem; the response carries fresh tokens with the new org_id claim."""
    from datetime import datetime, timedelta
    import secrets

    # Mint an invite for org_b's id, no email lock for simplicity
    async with AsyncSessionLocal() as db:
        inv = OrgInvite(
            token=f"REDEEM-{secrets.token_urlsafe(12)}",
            organization_id=org_b.org_id,
            note="cross-org test",
            expires_at=datetime.utcnow() + timedelta(days=1),
            created_by="test",
        )
        db.add(inv)
        await db.commit()
        token = inv.token

    # User in org A redeems
    resp = await client.post(
        "/api/auth/redeem-invite",
        json={"invite_token": token},
        headers=org_a.headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["organization_slug"]
    assert body["access_token"]
    assert body["refresh_token"]

    # Decode the new access_token's org_id claim and confirm it points to org_b
    from app.auth.security import decode_token
    claims = decode_token(body["access_token"])
    assert claims["org_id"] == org_b.org_id

    # Invite is now used
    async with AsyncSessionLocal() as db:
        from sqlalchemy import select
        again = (await db.execute(
            select(OrgInvite).where(OrgInvite.token == token)
        )).scalar_one()
        assert again.used_at is not None
        assert again.used_by == org_a.username


async def test_redeem_invite_rejects_used_token(client, org_a) -> None:
    """Single-use: the same token can't be redeemed twice."""
    from datetime import datetime, timedelta
    import secrets

    async with AsyncSessionLocal() as db:
        inv = OrgInvite(
            token=f"REDEEM-{secrets.token_urlsafe(12)}",
            organization_id=org_a.org_id,
            used_at=datetime.utcnow(),
            used_by="someone-else",
            expires_at=datetime.utcnow() + timedelta(days=1),
        )
        db.add(inv)
        await db.commit()
        token = inv.token

    resp = await client.post(
        "/api/auth/redeem-invite",
        json={"invite_token": token},
        headers=org_a.headers,
    )
    assert resp.status_code == 400
    assert "已被使用" in resp.json()["detail"]


# ── helper ─────────────────────────────────────────────────────────────

async def _get_org_slug(org_id: str) -> str:
    async with AsyncSessionLocal() as db:
        org = await db.get(Organization, org_id)
        return org.slug
