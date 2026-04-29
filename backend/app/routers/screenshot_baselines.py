"""每個 step 的 screenshot diff baseline CRUD。

key 是 step UUID（即 ``testcase_contents.steps_json[i].id``）；前端在使用者按
「設為基準」時呼叫此 API：
  - PUT （上傳檔案 multipart）：把使用者選的 PNG 存到 MinIO 並寫進 DB
  - GET ：回傳當前 baseline URL 與門檻
  - DELETE：移除 baseline，下次執行時 listener 會 auto-save 當下截圖當新 baseline
"""
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.auth.scope import ensure_object_in_scope_via_parent, ensure_project_in_scope
from app.database import get_db
from app.models.step_screenshot_baseline import StepScreenshotBaseline
from app.models.tree_node import TreeNode
from app.models.user import User
from app.services.storage_service import save_bytes

router = APIRouter()


async def _check_baseline_scope(
    obj: Optional[StepScreenshotBaseline], user: User, db: AsyncSession
) -> None:
    """Baselines do not store project_id directly. They link to tree_nodes via
    testcase_node_id; resolve the org through that parent. Rows with no parent
    link cannot be scope-checked here, so we treat them as visible only to
    superusers (defensive default — unscoped data should not leak)."""
    if obj is None or obj.testcase_node_id is None:
        if not user.is_superuser:
            raise HTTPException(status_code=404, detail="Baseline not found")
        return
    await ensure_object_in_scope_via_parent(
        db, TreeNode, obj.testcase_node_id, user,
        not_found_detail="Baseline not found",
    )


@router.get("/steps/{step_uuid}/baseline", response_model=Optional[BaselineResponse])
async def get_baseline(
    step_uuid: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    obj = await db.get(StepScreenshotBaseline, step_uuid)
    if obj is None:
        return None  # 200 with body null
    await _check_baseline_scope(obj, user, db)
    return obj


@router.put("/steps/{step_uuid}/baseline", response_model=BaselineResponse)
async def upsert_baseline(
    step_uuid: str,
    file: UploadFile = File(...),
    threshold_pct: float = Form(1.0),
    testcase_node_id: Optional[str] = Form(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """上傳新基準圖(PNG / JPEG / WebP);既有 baseline 直接覆蓋。"""
    # 須提供 testcase_node_id 以驗證 org 範圍(無此欄位則僅限 superuser)。
    if testcase_node_id:
        await ensure_object_in_scope_via_parent(
            db, TreeNode, testcase_node_id, user,
            not_found_detail="Testcase node not found",
        )
    elif not user.is_superuser:
        raise HTTPException(
            status_code=400,
            detail="testcase_node_id is required for baseline upload outside superuser context",
        )

    if (file.content_type or "").lower() not in {"image/png", "image/jpeg", "image/webp"}:
        raise HTTPException(status_code=400, detail=f"不支援的 image content type: {file.content_type}")

    data = await file.read()
    if len(data) > 10 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="檔案超過 10MB 上限")

    # 統一存成 .png（即使來源是 jpeg/webp 也照原 bytes 存，副檔名僅供識別）
    key = f"baselines/{step_uuid}.png"
    url = save_bytes(data, key, bucket="results", content_type="image/png")

    obj = await db.get(StepScreenshotBaseline, step_uuid)
    if obj is None:
        obj = StepScreenshotBaseline(
            step_uuid=step_uuid,
            testcase_node_id=testcase_node_id,
            baseline_url=url,
            threshold_pct=float(threshold_pct or 1.0),
        )
        db.add(obj)
    else:
        obj.baseline_url = url
        obj.threshold_pct = float(threshold_pct or 1.0)
        if testcase_node_id is not None:
            obj.testcase_node_id = testcase_node_id
    await db.flush()
    await db.refresh(obj)
    return obj


class CopyFromUrlRequest(BaseModel):
    """把已經在 MinIO 上的某張截圖（執行報告內的 actual / pre / post）直接設為 baseline。"""
    source_url: str
    threshold_pct: float = 1.0
    testcase_node_id: Optional[str] = None


@router.post("/steps/{step_uuid}/baseline/copy-from", response_model=BaselineResponse)
async def copy_from(
    step_uuid: str,
    payload: CopyFromUrlRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """從現有 URL 複製成 baseline(透過 backend 從 MinIO 讀後再存到 baseline key)。"""
    if payload.testcase_node_id:
        await ensure_object_in_scope_via_parent(
            db, TreeNode, payload.testcase_node_id, user,
            not_found_detail="Testcase node not found",
        )
    elif not user.is_superuser:
        raise HTTPException(
            status_code=400,
            detail="testcase_node_id is required for baseline copy-from outside superuser context",
        )
    import re

    import httpx  # FastAPI 已經依賴 httpx

    # 解析 source_url，從 /results/<key> 取出 key 直接從 MinIO 讀（避免穿過 nginx 多繞一圈）
    m = re.match(r"^https?://[^/]+(/results/.+)$", payload.source_url) or re.match(
        r"^(/results/.+)$", payload.source_url
    )
    if not m:
        raise HTTPException(status_code=400, detail=f"source_url 必須是 /results/... 形式，收到 {payload.source_url}")
    key = m.group(1)[len("/results/"):]

    from app.config import settings
    if (settings.STORAGE_BACKEND or "local").lower() != "minio":
        raise HTTPException(status_code=400, detail="copy-from 目前只支援 STORAGE_BACKEND=minio")

    import boto3  # type: ignore
    s3 = boto3.client(
        "s3",
        endpoint_url=settings.MINIO_ENDPOINT,
        aws_access_key_id=settings.MINIO_ACCESS_KEY,
        aws_secret_access_key=settings.MINIO_SECRET_KEY,
        region_name="us-east-1",
    )
    try:
        obj_get = s3.get_object(Bucket="results", Key=key)
        data = obj_get["Body"].read()
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"無法從 MinIO 讀取 source_url: {e}")

    new_key = f"baselines/{step_uuid}.png"
    url = save_bytes(data, new_key, bucket="results", content_type="image/png")

    obj = await db.get(StepScreenshotBaseline, step_uuid)
    if obj is None:
        obj = StepScreenshotBaseline(
            step_uuid=step_uuid,
            testcase_node_id=payload.testcase_node_id,
            baseline_url=url,
            threshold_pct=float(payload.threshold_pct or 1.0),
        )
        db.add(obj)
    else:
        obj.baseline_url = url
        obj.threshold_pct = float(payload.threshold_pct or 1.0)
        if payload.testcase_node_id is not None:
            obj.testcase_node_id = payload.testcase_node_id
    await db.flush()
    await db.refresh(obj)
    return obj


@router.delete("/steps/{step_uuid}/baseline", status_code=204)
async def delete_baseline(
    step_uuid: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    obj = await db.get(StepScreenshotBaseline, step_uuid)
    if obj is None:
        return  # 204 anyway
    await _check_baseline_scope(obj, user, db)
    # 不主動刪除 MinIO 上的物件(萬一其他 report 還引用);只移除 DB 紀錄
    await db.delete(obj)
    await db.flush()
