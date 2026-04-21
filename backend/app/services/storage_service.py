"""Storage abstraction.

Two backends are supported, switched via ``STORAGE_BACKEND`` env var:

* ``local`` — write to ``settings.PIC_FOLDER`` and serve via FastAPI/nginx
  ``/pics/`` mount. Default for dev quick-start.
* ``minio`` — write to a MinIO bucket via the S3-compatible API (boto3).
  Files are served back to the user via nginx ``location /pics/`` and
  ``/results/`` reverse proxies pointing at MinIO. The DB only stores the
  relative URL (``/pics/<key>`` or ``/results/<key>``) so the platform
  remains agnostic to the actual host.

Public surface:

* ``save_screenshot(file)`` — used by the ``/api/upload`` endpoint.
* ``save_bytes(data, key, bucket, content_type)`` — used by the Celery
  Robot listener to persist per-step screenshots and the final
  ``log.html`` / ``report.html``.
"""

from __future__ import annotations

import io
import os
import uuid
from typing import Literal, Protocol

import aiofiles
from fastapi import HTTPException, UploadFile

from app.config import settings

ALLOWED_MIME = {"image/png", "image/jpeg", "image/webp"}
MAX_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB

BucketName = Literal["pic", "results"]
_BUCKET_TO_URL_PREFIX: dict[str, str] = {"pic": "/pics", "results": "/results"}


class _StorageBackend(Protocol):
    async def put_upload(self, file: UploadFile, bucket: BucketName, key: str) -> str: ...
    def put_bytes(self, data: bytes, bucket: BucketName, key: str, content_type: str) -> str: ...


# ── Local filesystem backend ──────────────────────────────────────────


class _LocalStorage:
    """Write files under ``settings.PIC_FOLDER/<bucket>/<key>``."""

    def __init__(self, root: str) -> None:
        self._root = root

    def _full_path(self, bucket: BucketName, key: str) -> str:
        path = os.path.join(self._root, bucket, key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        return path

    async def put_upload(self, file: UploadFile, bucket: BucketName, key: str) -> str:
        content = await file.read()
        if len(content) > MAX_SIZE_BYTES:
            raise HTTPException(status_code=413, detail="檔案超過 10 MB 上限")
        async with aiofiles.open(self._full_path(bucket, key), "wb") as f:
            await f.write(content)
        return f"{_BUCKET_TO_URL_PREFIX[bucket]}/{key}"

    def put_bytes(self, data: bytes, bucket: BucketName, key: str, content_type: str) -> str:
        with open(self._full_path(bucket, key), "wb") as f:
            f.write(data)
        return f"{_BUCKET_TO_URL_PREFIX[bucket]}/{key}"


# ── MinIO (S3-compatible) backend ─────────────────────────────────────


class _MinioStorage:
    def __init__(self) -> None:
        # boto3 是可選依賴：只有 STORAGE_BACKEND=minio 時才會被載入
        import boto3  # type: ignore[import-not-found]
        from botocore.client import Config  # type: ignore[import-not-found]

        self._client = boto3.client(
            "s3",
            endpoint_url=settings.MINIO_ENDPOINT,
            aws_access_key_id=settings.MINIO_ACCESS_KEY,
            aws_secret_access_key=settings.MINIO_SECRET_KEY,
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


def _build_backend() -> _StorageBackend:
    backend = (settings.STORAGE_BACKEND or "local").lower()
    if backend == "minio":
        return _MinioStorage()
    return _LocalStorage(settings.PIC_FOLDER)


_backend: _StorageBackend = _build_backend()


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
import os
import uuid

import aiofiles
from fastapi import HTTPException, UploadFile

from app.config import settings

ALLOWED_MIME = {"image/png", "image/jpeg", "image/webp"}
MAX_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB


async def save_screenshot(file: UploadFile) -> str:
    """
    將上傳的截圖存到 PIC 資料夾，回傳可公開存取的 URL。
    ── 安全措施 ──────────────────────────────────────────────
    1. 白名單 MIME 驗證（拒絕非圖片檔）
    2. 強制覆寫副檔名（防止副檔名偽裝）
    3. UUID 隨機檔名（防路徑遍歷攻擊）
    4. 10 MB 大小限制
    """
    if file.content_type not in ALLOWED_MIME:
        raise HTTPException(
            status_code=415,
            detail=f"不支援的檔案類型：{file.content_type}，請上傳 PNG / JPEG / WebP",
        )

    ext_map = {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/webp": ".webp",
    }
    ext = ext_map[file.content_type]  # type: ignore[index]
    filename = f"{uuid.uuid4()}{ext}"

    os.makedirs(settings.PIC_FOLDER, exist_ok=True)
    filepath = os.path.join(settings.PIC_FOLDER, filename)

    content = await file.read()
    if len(content) > MAX_SIZE_BYTES:
        raise HTTPException(status_code=413, detail="截圖檔案超過 10 MB 上限")

    async with aiofiles.open(filepath, "wb") as f:
        await f.write(content)

    return f"{settings.BASE_URL}/pics/{filename}"
