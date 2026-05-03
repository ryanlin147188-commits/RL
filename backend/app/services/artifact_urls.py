"""Short-lived artifact URL signing.

Execution screenshots, videos, Robot reports, and traces are stored in S3
compatible buckets but exposed through same-origin ``/pics`` and ``/results``
paths. The browser often loads those paths from ``<img>``, ``<video>``, or an
external trace viewer where an Authorization header is unavailable, so we use a
scoped, short-lived token instead of leaking the user's access token.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import jwt

from app.auth.security import JWT_ALGORITHM, JWT_SECRET

ArtifactBucket = Literal["pic", "results"]


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def split_artifact_path(url_or_path: str) -> tuple[ArtifactBucket, str] | None:
    """Return ``(bucket, key)`` for ``/pics/<key>`` or ``/results/<key>`` URLs."""
    if not url_or_path:
        return None
    parsed = urlsplit(url_or_path)
    path = parsed.path or url_or_path
    if path.startswith("/pics/"):
        return "pic", path[len("/pics/") :]
    if path.startswith("/results/"):
        return "results", path[len("/results/") :]
    return None


def create_artifact_token(bucket: ArtifactBucket, key: str, *, ttl_seconds: int = 3600) -> str:
    payload = {
        "typ": "artifact",
        "bucket": bucket,
        "key": key,
        "iat": _now_utc(),
        "exp": _now_utc() + timedelta(seconds=ttl_seconds),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def sign_artifact_url(url_or_path: str | None, *, ttl_seconds: int = 3600) -> str | None:
    """Append an artifact-scoped token to a storage URL/path.

    Non-artifact URLs are returned unchanged so callers can safely map every
    optional URL field without branching.
    """
    if not url_or_path:
        return url_or_path
    parts = split_artifact_path(url_or_path)
    if not parts:
        return url_or_path
    bucket, key = parts
    if not key or key.startswith("/") or ".." in key.split("/"):
        return url_or_path

    parsed = urlsplit(url_or_path)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["artifact_token"] = create_artifact_token(bucket, key, ttl_seconds=ttl_seconds)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))
