"""Storage abstraction.

Single backend: ``s3`` — write to a SeaweedFS bucket via the S3-compatible
API (boto3). Files are served back to the user via authenticated backend
proxy routes for ``/pics/<key>`` and ``/results/<key>``. The DB only
stores the relative URL so the platform stays agnostic to the actual host.

``STORAGE_BACKEND`` env var must be ``s3``. Earlier versions accepted
``local`` (filesystem) and ``minio`` (alias for the S3 path); both have
been removed — uploads must persist to SeaweedFS, not the container's
local filesystem (which would be wiped on restart) and not a Minio name
that no longer reflects the actual implementation.

Public surface:

* ``save_screenshot(file)`` — used by the ``/api/upload`` endpoint.
* ``save_bytes(data, key, bucket, content_type)`` — used by the Celery
  Robot listener to persist per-step screenshots and the final
  ``log.html`` / ``report.html``.
* ``fetch_bytes(bucket, key)`` — read back via the artifact proxy.
* ``save_upload(file, bucket, key)`` — generic UploadFile sink.
"""

from __future__ import annotations

import io
import uuid
from typing import Literal, Protocol

from fastapi import HTTPException, UploadFile

from app.config import settings

ALLOWED_MIME = {"image/png", "image/jpeg", "image/webp"}
MAX_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB

BucketName = Literal["pic", "results"]
_BUCKET_TO_URL_PREFIX: dict[str, str] = {"pic": "/pics", "results": "/results"}


class _StorageBackend(Protocol):
    async def put_upload(self, file: UploadFile, bucket: BucketName, key: str) -> str: ...
    def put_bytes(self, data: bytes, bucket: BucketName, key: str, content_type: str) -> str: ...
    def fetch_bytes(self, bucket: BucketName, key: str) -> bytes: ...


# ── S3-compatible backend (SeaweedFS) ─────────────────────────────────


class _S3Storage:
    def __init__(self) -> None:
        import boto3  # type: ignore[import-not-found]
        from botocore.client import Config  # type: ignore[import-not-found]

        self._client = boto3.client(
            "s3",
            endpoint_url=settings.S3_ENDPOINT,
            aws_access_key_id=settings.S3_ACCESS_KEY,
            aws_secret_access_key=settings.S3_SECRET_KEY,
            config=Config(signature_version="s3v4"),
            region_name="us-east-1",
        )

    async def put_upload(self, file: UploadFile, bucket: BucketName, key: str) -> str:
        content = await file.read()
        if len(content) > MAX_SIZE_BYTES:
            raise HTTPException(status_code=413, detail="檔案超過 10 MB 上限")
        self._client.put_object(
            Bucket=bucket,
            Key=key,
            Body=content,
            ContentType=file.content_type or "application/octet-stream",
        )
        return f"{_BUCKET_TO_URL_PREFIX[bucket]}/{key}"

    def put_bytes(self, data: bytes, bucket: BucketName, key: str, content_type: str) -> str:
        self._client.put_object(Bucket=bucket, Key=key, Body=io.BytesIO(data), ContentType=content_type)
        return f"{_BUCKET_TO_URL_PREFIX[bucket]}/{key}"

    def fetch_bytes(self, bucket: BucketName, key: str) -> bytes:
        try:
            obj = self._client.get_object(Bucket=bucket, Key=key)
            return obj["Body"].read()
        except Exception as e:
            raise HTTPException(404, f"object not found: {bucket}/{key} ({e})")


def _build_backend() -> _StorageBackend:
    backend = (settings.STORAGE_BACKEND or "").lower()
    if backend != "s3":
        raise RuntimeError(
            f"STORAGE_BACKEND='{settings.STORAGE_BACKEND}' is not supported. "
            f"This deploy only supports STORAGE_BACKEND=s3 (SeaweedFS via "
            f"S3-compatible API). The 'local' / 'minio' values from earlier "
            f"versions have been removed."
        )
    return _S3Storage()


_backend: _StorageBackend = _build_backend()


def ensure_buckets() -> None:
    """Idempotently create the canonical buckets (pic, results) on the S3 backend.

    取代 v1.1.x 的獨立 ``seaweedfs-init`` compose service(原本只為了跑兩行
    ``aws s3 mb`` 就拉一份 amazon/aws-cli image,~500MB)。backend 開機時用
    既有的 boto3 client 自己建,seaweedfs healthcheck 通過後就能呼叫。
    """
    from botocore.exceptions import ClientError  # type: ignore[import-not-found]

    if not isinstance(_backend, _S3Storage):
        return
    for bucket in ("pic", "results"):
        try:
            _backend._client.create_bucket(Bucket=bucket)
        except ClientError as e:
            code = (e.response.get("Error", {}) or {}).get("Code", "")
            if code in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
                continue
            raise


# ── Public API ────────────────────────────────────────────────────────


async def save_screenshot(file: UploadFile) -> str:
    """Validate + persist user-uploaded screenshot, return public relative URL."""
    if file.content_type not in ALLOWED_MIME:
        raise HTTPException(
            status_code=415,
            detail=f"不支援的檔案類型：{file.content_type}，請上傳 PNG / JPEG / WebP",
        )

    ext_map = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}
    ext = ext_map[file.content_type]  # type: ignore[index]
    key = f"{uuid.uuid4()}{ext}"
    return await _backend.put_upload(file, "pic", key)


def save_bytes(data: bytes, key: str, *, bucket: BucketName = "results", content_type: str = "application/octet-stream") -> str:
    """Persist arbitrary bytes (used by Robot listener for screenshots / reports)."""
    return _backend.put_bytes(data, bucket, key, content_type)


def fetch_bytes(bucket: BucketName, key: str) -> bytes:
    """Read bytes back from SeaweedFS via the artifact proxy."""
    return _backend.fetch_bytes(bucket, key)


async def save_upload(file: UploadFile, *, bucket: BucketName = "pic", key: str | None = None) -> str:
    """Persist a generic UploadFile to SeaweedFS.

    Returns the relative URL (``/pics/<key>`` or ``/results/<key>``).
    """
    if key is None:
        ext = ""
        if file.filename and "." in file.filename:
            ext = "." + file.filename.rsplit(".", 1)[-1]
        key = f"{uuid.uuid4()}{ext}"
    return await _backend.put_upload(file, bucket, key)
