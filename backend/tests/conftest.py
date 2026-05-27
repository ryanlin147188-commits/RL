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
# config.py 的 DB_PASSWORD / S3_SECRET_KEY 自 v1.1.13 起為必填;測試用隨機值
# 避免雖然 unit test 不連 DB 但 import app.config 時觸發 ValidationError。
# integration testcontainer fixture 會再以實際 container password 覆寫。
os.environ.setdefault("DB_PASSWORD", secrets.token_hex(16))
os.environ.setdefault("S3_SECRET_KEY", secrets.token_hex(16))
# v1.1.14 P1-4:lifespan 內 _ensure_default_admin() 需要 admin 初始密碼,
# 不能為 admin123 / changeme / password / secret 等弱值。隨機 hex 用於 integration test。
os.environ.setdefault("AUTOTEST_DEFAULT_ADMIN_PASSWORD", secrets.token_hex(16))
