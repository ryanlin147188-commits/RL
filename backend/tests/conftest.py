"""Pytest fixtures shared by all tests.

Only env setup lives here so that ``tests/unit`` never imports a DB dependency.
DB / app fixtures (testcontainers, AsyncClient, org factories) live in
``tests/integration/conftest.py`` and apply only to that subtree.

Env vars are set BEFORE the app is imported so module-level secret checks
(``app.auth.security.JWT_SECRET``, ``crypto._build_fernet``) do not raise.
"""
from __future__ import annotations

import base64
import os
import secrets


# ── Env setup — must happen before anything imports the app package ──────
os.environ.setdefault("AUTOTEST_JWT_SECRET", secrets.token_hex(32))
os.environ.setdefault(
    "AUTOTEST_FERNET_KEY",
    base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii"),
)
os.environ.setdefault("ALLOWED_ORIGINS", "http://test")
os.environ.setdefault("DEBUG", "False")
os.environ.setdefault("AUTOTEST_TEST_MODE", "1")
