"""JWT round-trip + rejection tests — pure unit, no DB."""
from __future__ import annotations

import time

import jwt as pyjwt
import pytest

from app.auth.security import (
    JWT_ALGORITHM,
    JWT_SECRET,
    create_access_token,
    create_refresh_token,
    decode_token,
)


def test_access_token_round_trip() -> None:
    token = create_access_token("alice", extra={"org_id": "org-1", "is_superuser": False})
    payload = decode_token(token)
    assert payload["sub"] == "alice"
    assert payload["typ"] == "access"
    assert payload["org_id"] == "org-1"
    assert payload["is_superuser"] is False


def test_refresh_token_typ() -> None:
    payload = decode_token(create_refresh_token("alice"))
    assert payload["typ"] == "refresh"


def test_decode_rejects_tampered_signature() -> None:
    token = create_access_token("alice")
    tampered = token[:-2] + ("aa" if not token.endswith("aa") else "bb")
    with pytest.raises(pyjwt.InvalidSignatureError):
        decode_token(tampered)


def test_decode_rejects_expired_token() -> None:
    expired = pyjwt.encode(
        {"sub": "alice", "exp": int(time.time()) - 10, "typ": "access"},
        JWT_SECRET,
        algorithm=JWT_ALGORITHM,
    )
    with pytest.raises(pyjwt.ExpiredSignatureError):
        decode_token(expired)
