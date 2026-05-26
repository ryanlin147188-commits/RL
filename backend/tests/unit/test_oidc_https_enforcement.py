"""OIDC HTTPS 強制測試。

v1.1.13 起:
  * BASE_URL 在非 localhost 必須 https://(否則 Settings 啟動 fail)
  * oidc._resolve_redirect_uri 對 ZOHO_REDIRECT_URL 套同樣規則
"""
from __future__ import annotations

import secrets

import pytest
from pydantic import ValidationError

from app.config import Settings


def _kw():
    return {"DB_PASSWORD": secrets.token_hex(16), "S3_SECRET_KEY": secrets.token_hex(16)}


# ── BASE_URL validator ───────────────────────────────────────────────────


@pytest.mark.parametrize("url", [
    "https://example.com",
    "http://localhost",
    "http://localhost:8000",
    "http://127.0.0.1:8000",
    "http://app.localhost",
    # v1.1.13:RFC1918 私有網段允許 http(內網部署常見場景)
    "http://192.168.4.89",
    "http://10.0.5.1:8000",
    "http://172.20.0.10",
])
def test_base_url_accepted(url: str):
    s = Settings(BASE_URL=url, **_kw())
    assert s.BASE_URL == url


@pytest.mark.parametrize("url", [
    "http://example.com",
    "http://8.8.8.8",
    "http://prod.internal",
    "http://172.32.0.10",  # 不在 RFC1918 範圍
])
def test_base_url_http_in_public_rejected(url: str):
    with pytest.raises(ValidationError) as ei:
        Settings(BASE_URL=url, **_kw())
    assert "HTTPS" in str(ei.value) or "https" in str(ei.value)


def test_base_url_invalid_scheme_rejected():
    with pytest.raises(ValidationError):
        Settings(BASE_URL="ftp://example.com", **_kw())


# ── oidc._resolve_redirect_uri ───────────────────────────────────────────


def test_resolve_redirect_uri_derives_from_base_url(monkeypatch):
    monkeypatch.delenv("ZOHO_REDIRECT_URL", raising=False)
    # 暫時把 settings 換成 https BASE_URL
    from app import config as cfg_mod
    from app.auth import oidc as oidc_mod

    orig = cfg_mod.settings.BASE_URL
    try:
        object.__setattr__(cfg_mod.settings, "BASE_URL", "https://prod.example.com")
        uri = oidc_mod._resolve_redirect_uri("zoho", "ZOHO_REDIRECT_URL", "/api/auth/zoho/callback")
        assert uri == "https://prod.example.com/api/auth/zoho/callback"
    finally:
        object.__setattr__(cfg_mod.settings, "BASE_URL", orig)


def test_resolve_redirect_uri_rejects_http_on_public(monkeypatch):
    from app.auth import oidc as oidc_mod

    monkeypatch.setenv("ZOHO_REDIRECT_URL", "http://prod.example.com/api/auth/zoho/callback")
    with pytest.raises(RuntimeError) as ei:
        oidc_mod._resolve_redirect_uri("zoho", "ZOHO_REDIRECT_URL", "/api/auth/zoho/callback")
    assert "HTTPS" in str(ei.value)


def test_resolve_redirect_uri_allows_localhost_http(monkeypatch):
    from app.auth import oidc as oidc_mod

    monkeypatch.setenv("ZOHO_REDIRECT_URL", "http://localhost/api/auth/zoho/callback")
    uri = oidc_mod._resolve_redirect_uri("zoho", "ZOHO_REDIRECT_URL", "/api/auth/zoho/callback")
    assert uri.startswith("http://localhost")


def test_resolve_redirect_uri_allows_rfc1918_http(monkeypatch):
    """v1.1.13:RFC1918 私有 IP 內網部署允許 http(例:192.168.4.89)。"""
    from app.auth import oidc as oidc_mod

    monkeypatch.setenv("ZOHO_REDIRECT_URL", "http://192.168.4.89/api/auth/zoho/callback")
    uri = oidc_mod._resolve_redirect_uri("zoho", "ZOHO_REDIRECT_URL", "/api/auth/zoho/callback")
    assert uri == "http://192.168.4.89/api/auth/zoho/callback"
