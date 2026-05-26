"""Settings 驗證測試:DB_PASSWORD / S3_SECRET_KEY 必填 + 弱密碼防呆。

config.py 自 v1.1.13 起把這兩個欄位改為 Field(...) 必填,並加上
field_validator 拒絕 admin123 / changeme / password / secret 等已知弱值。
"""
from __future__ import annotations

import secrets

import pytest
from pydantic import ValidationError

from app.config import Settings


def _strong() -> str:
    return secrets.token_hex(16)


def test_settings_loads_with_strong_secrets():
    s = Settings(DB_PASSWORD=_strong(), S3_SECRET_KEY=_strong())
    assert s.DB_PASSWORD
    assert s.S3_SECRET_KEY


@pytest.mark.parametrize("weak", ["admin123", "ADMIN123", "changeme", "password", "secret"])
def test_rejects_known_weak_db_password(weak: str):
    with pytest.raises(ValidationError) as ei:
        Settings(DB_PASSWORD=weak, S3_SECRET_KEY=_strong())
    assert "DB_PASSWORD" in str(ei.value)


@pytest.mark.parametrize("weak", ["admin123", "changeme", "password", "secret"])
def test_rejects_known_weak_s3_secret(weak: str):
    with pytest.raises(ValidationError) as ei:
        Settings(DB_PASSWORD=_strong(), S3_SECRET_KEY=weak)
    assert "S3_SECRET_KEY" in str(ei.value)


def test_rejects_short_db_password():
    with pytest.raises(ValidationError):
        Settings(DB_PASSWORD="short", S3_SECRET_KEY=_strong())


def test_rejects_short_s3_secret():
    with pytest.raises(ValidationError):
        Settings(DB_PASSWORD=_strong(), S3_SECRET_KEY="short")
