"""Security regressions for settings, secrets, and DB connection config."""
from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.integration


async def test_viewer_cannot_mutate_sensitive_settings(client, viewer_in_a, org_a) -> None:
    cases = [
        (
            "post",
            "/api/settings/roles",
            {"name": f"viewer-role-{uuid.uuid4().hex[:6]}", "permissions_json": []},
        ),
        (
            "put",
            "/api/settings/email",
            {"smtp_host": "smtp.example.com", "enabled": True},
        ),
        (
            "post",
            "/api/settings/ai-tokens",
            {"name": "blocked", "provider": "OpenAI", "api_key": "sk-test"},
        ),
        (
            "post",
            "/api/db-configs",
            {
                "project_id": org_a.project_id,
                "name": f"blocked_{uuid.uuid4().hex[:6]}",
                "db_type": "postgresql",
                "password": "secret",
            },
        ),
    ]
    for method, url, body in cases:
        resp = await getattr(client, method)(url, json=body, headers=viewer_in_a.headers)
        assert resp.status_code == 403, f"{method.upper()} {url} should be forbidden"


async def test_sensitive_settings_responses_redact_secrets(client, admin_in_a, org_a) -> None:
    email_update = await client.put(
        "/api/settings/email",
        json={
            "smtp_host": "smtp.example.com",
            "smtp_port": 587,
            "smtp_user": "mailer",
            "smtp_password": "smtp-secret",
            "enabled": True,
        },
        headers=admin_in_a.headers,
    )
    assert email_update.status_code == 200
    email_body = email_update.json()
    assert email_body["smtp_password"] is None
    assert email_body["has_smtp_password"] is True

    ai_create = await client.post(
        "/api/settings/ai-tokens",
        json={
            "name": "redaction-openai",
            "provider": "OpenAI",
            "api_key": "sk-secret",
            "model": "gpt-test",
        },
        headers=admin_in_a.headers,
    )
    assert ai_create.status_code == 201
    ai_body = ai_create.json()
    assert ai_body["api_key"] is None
    assert ai_body["has_api_key"] is True

    db_create = await client.post(
        "/api/db-configs",
        json={
            "project_id": org_a.project_id,
            "name": f"db_{uuid.uuid4().hex[:8]}",
            "db_type": "postgresql",
            "host": "postgres",
            "username": "app",
            "password": "db-secret",
        },
        headers=admin_in_a.headers,
    )
    assert db_create.status_code == 201
    db_body = db_create.json()
    assert db_body["password"] is None
    assert db_body["has_password"] is True


async def test_empty_secret_update_preserves_existing_value(client, admin_in_a, org_a) -> None:
    created = await client.post(
        "/api/db-configs",
        json={
            "project_id": org_a.project_id,
            "name": f"preserve_{uuid.uuid4().hex[:8]}",
            "db_type": "postgresql",
            "password": "original-secret",
        },
        headers=admin_in_a.headers,
    )
    assert created.status_code == 201
    cfg_id = created.json()["id"]

    updated = await client.put(
        f"/api/db-configs/{cfg_id}",
        json={"description": "changed", "password": None},
        headers=admin_in_a.headers,
    )
    assert updated.status_code == 200
    assert updated.json()["password"] is None
    assert updated.json()["has_password"] is True
