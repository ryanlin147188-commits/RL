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

# v1.1.15 P2-3:圖片檔頭簽章。client-side 宣告的 content_type 可任意偽造,
# 上傳檔案前要從實際 byte 推回真實格式,避免「.png 宣告但實際是 PHP shell」
# 之類的攻擊。表格 keyed by canonical MIME。
#
# 格式:
#   * bytes  → 從 offset 0 開始比對的固定 prefix
#   * tuple  → (prefix, offset, tail) 用於 RIFF / WEBP 這類兩段式 magic
_IMAGE_MAGIC: dict[str, object] = {
    "image/png":  b"\x89PNG\r\n\x1a\n",
    "image/jpeg": b"\xff\xd8\xff",            # JPEG SOI marker(後 byte 為 markers E0/E1/...)
    "image/webp": (b"RIFF", 8, b"WEBP"),      # RIFF + 4 bytes size + "WEBP"
    "image/gif":  b"GIF8",                    # GIF87a / GIF89a 共通開頭
}


def _matches_magic(data: bytes, magic: object) -> bool:
    if isinstance(magic, bytes):
        return data.startswith(magic)
    if isinstance(magic, tuple):
        prefix, offset, tail = magic  # type: ignore[misc]
        return data.startswith(prefix) and data[offset:offset + len(tail)] == tail
    return False


def detect_image_type(data: bytes, allowed_types: set[str]) -> str:
    """從 byte 內容推回真實 MIME。不在 ``allowed_types`` 中 → 415。

    v1.1.15 P2-3:不信任 client 宣告的 content_type,改從 magic bytes 推。
    撞到偽造副檔名 / MIME 攻擊時直接擋。
    """
    if not data:
        raise HTTPException(status_code=400, detail="檔案為空")
    for ct, magic in _IMAGE_MAGIC.items():
        if ct not in allowed_types:
            continue
        if _matches_magic(data, magic):
            return ct
    raise HTTPException(
        status_code=415,
        detail=f"檔案內容不符任何允許的圖片格式({', '.join(sorted(allowed_types))})",
    )

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
        except Exception as e:  # noqa: BLE001
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
    """Validate + persist user-uploaded screenshot, return public relative URL.

    v1.1.15 P2-3:不再相信 client 宣告的 ``file.content_type``,改讀 byte
    用 magic bytes 推真實格式;偽造 MIME 上傳 PHP shell 之類的會在這層被擋。
    """
    raw = await file.read()
    if len(raw) > MAX_SIZE_BYTES:
        raise HTTPException(status_code=413, detail="檔案超過 10 MB 上限")
    real_ct = detect_image_type(raw, ALLOWED_MIME)
    ext_map = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}
    ext = ext_map[real_ct]
    key = f"{uuid.uuid4()}{ext}"
    return _backend.put_bytes(raw, "pic", key, real_ct)


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
