"""Defect 缺陷管理 REST endpoints。"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.database import get_db
from app.models.defect import Defect, DefectSeverity, DefectPriority, DefectStatus
from app.schemas.defect import (
    AttachmentResponse,
    DefectCreate,
    DefectResponse,
    DefectUpdate,
)
from app.services.storage_service import _backend  # 直接重用底層 storage backend

router = APIRouter()


async def _next_code(db: AsyncSession, project_id: str) -> str:
    """產生下一個 BUG-NNN code（同專案內遞增）。"""
    result = await db.execute(
        select(func.count(Defect.id)).where(Defect.project_id == project_id)
    )
    n = (result.scalar_one_or_none() or 0) + 1
    return f"BUG-{n:03d}"


def _resolve_enum(enum_cls, val, default):
    if val is None:
        return default
    try:
        return enum_cls(val)
    except ValueError:
        return default


@router.get("/defects", response_model=list[DefectResponse], tags=["L · 缺陷管理"])
async def list_defects(
    project_id: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    severity: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(Defect).order_by(desc(Defect.created_at))
    if project_id:
        stmt = stmt.where(Defect.project_id == project_id)
    if status:
        stmt = stmt.where(Defect.status == DefectStatus(status))
    if severity:
        stmt = stmt.where(Defect.severity == DefectSeverity(severity))
    rows = (await db.execute(stmt)).scalars().all()
    return list(rows)


@router.post("/defects", response_model=DefectResponse, status_code=201, tags=["L · 缺陷管理"])
async def create_defect(payload: DefectCreate, db: AsyncSession = Depends(get_db)):
    code = payload.code or await _next_code(db, payload.project_id)
    defect = Defect(
        project_id=payload.project_id,
        code=code,
        title=payload.title,
        description=payload.description,
        steps_to_reproduce=payload.steps_to_reproduce,
        expected_result=payload.expected_result,
        actual_result=payload.actual_result,
        severity=_resolve_enum(DefectSeverity, payload.severity, DefectSeverity.MINOR),
        priority=_resolve_enum(DefectPriority, payload.priority, DefectPriority.P2),
        status=_resolve_enum(DefectStatus, payload.status, DefectStatus.NEW),
        reporter=payload.reporter,
        assignee=payload.assignee,
        linked_testcase_id=payload.linked_testcase_id,
        linked_report_id=payload.linked_report_id,
        attachments_json=payload.attachments_json or [],
    )
    db.add(defect)
    await db.flush()
    await db.refresh(defect)
    return defect


@router.get("/defects/{defect_id}", response_model=DefectResponse, tags=["L · 缺陷管理"])
async def get_defect(defect_id: str, db: AsyncSession = Depends(get_db)):
    d = await db.get(Defect, defect_id)
    if not d:
        raise HTTPException(404, "Defect not found")
    return d


@router.put("/defects/{defect_id}", response_model=DefectResponse, tags=["L · 缺陷管理"])
async def update_defect(defect_id: str, payload: DefectUpdate, db: AsyncSession = Depends(get_db)):
    d = await db.get(Defect, defect_id)
    if not d:
        raise HTTPException(404, "Defect not found")
    data = payload.model_dump(exclude_unset=True)
    for key, val in data.items():
        if key == "severity" and val is not None:
            d.severity = _resolve_enum(DefectSeverity, val, d.severity)
        elif key == "priority" and val is not None:
            d.priority = _resolve_enum(DefectPriority, val, d.priority)
        elif key == "status" and val is not None:
            new_status = _resolve_enum(DefectStatus, val, d.status)
            d.status = new_status
            # 進入 Closed 自動標記 closed_at
            if new_status == DefectStatus.CLOSED and d.closed_at is None:
                d.closed_at = datetime.utcnow()
            elif new_status != DefectStatus.CLOSED and d.closed_at is not None:
                d.closed_at = None
        else:
            setattr(d, key, val)
    await db.flush()
    await db.refresh(d)
    return d


@router.delete("/defects/{defect_id}", status_code=204, tags=["L · 缺陷管理"])
async def delete_defect(defect_id: str, db: AsyncSession = Depends(get_db)):
    d = await db.get(Defect, defect_id)
    if not d:
        raise HTTPException(404, "Defect not found")
    await db.delete(d)
    await db.flush()


@router.get("/defects/stats/summary", tags=["L · 缺陷管理"])
async def defects_summary(
    project_id: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """供「測試看板」的 KPI cards 與 Kanban 列數使用。"""
    stmt = select(Defect.status, Defect.severity)
    if project_id:
        stmt = stmt.where(Defect.project_id == project_id)
    rows = (await db.execute(stmt)).all()
    by_status: dict[str, int] = {}
    by_severity: dict[str, int] = {}
    for status_val, severity_val in rows:
        s = status_val.value if hasattr(status_val, "value") else str(status_val)
        sv = severity_val.value if hasattr(severity_val, "value") else str(severity_val)
        by_status[s] = by_status.get(s, 0) + 1
        by_severity[sv] = by_severity.get(sv, 0) + 1
    return {
        "total": sum(by_status.values()),
        "by_status": by_status,
        "by_severity": by_severity,
    }


# ── 附件上傳 ──────────────────────────────────────────────────────────
# 允許上傳圖片或文件（PNG / JPEG / WebP / PDF / TXT / LOG）。檔案存到 storage
# 後端的 "pic" bucket，回傳的相對 URL 會 append 到該 defect 的 attachments_json。

ALLOWED_ATTACHMENT_MIME = {
    "image/png", "image/jpeg", "image/webp", "image/gif",
    "application/pdf",
    "text/plain", "text/csv", "application/json",
    "application/zip", "application/x-zip-compressed",
}
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024  # 20 MB


@router.post(
    "/defects/{defect_id}/attachments",
    response_model=AttachmentResponse,
    tags=["L · 缺陷管理"],
)
async def upload_defect_attachment(
    defect_id: str,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
):
    """為缺陷上傳一個附件（圖片／文件）。檔案會被持久化，URL 寫回 attachments_json 列表。"""
    d = await db.get(Defect, defect_id)
    if not d:
        raise HTTPException(404, "Defect not found")

    if file.content_type not in ALLOWED_ATTACHMENT_MIME:
        raise HTTPException(
            415,
            f"不支援的檔案類型：{file.content_type}",
        )

    # 讀內容檢查大小（put_upload 內也會檢查，但這裡先讀一次以拿到 size）
    content = await file.read()
    if len(content) > MAX_ATTACHMENT_BYTES:
        raise HTTPException(413, "附件超過 20 MB 上限")
    # 重設檔案游標讓 backend 可以再讀（部分 backend 會直接消化 UploadFile）
    await file.seek(0)

    ext = ""
    if "." in (file.filename or ""):
        ext = "." + file.filename.rsplit(".", 1)[-1].lower()
    key = f"defect_{defect_id}_{uuid.uuid4().hex}{ext or '.bin'}"
    url = await _backend.put_upload(file, "pic", key)

    attachment = {
        "name": file.filename or key,
        "url": url,
        "size": len(content),
        "type": file.content_type,
    }
    existing = list(d.attachments_json or [])
    existing.append(attachment)
    d.attachments_json = existing
    flag_modified(d, "attachments_json")
    await db.flush()
    return attachment


@router.delete(
    "/defects/{defect_id}/attachments/{index}",
    status_code=204,
    tags=["L · 缺陷管理"],
)
async def delete_defect_attachment(
    defect_id: str, index: int, db: AsyncSession = Depends(get_db)
):
    """從缺陷的 attachments_json 列表中移除第 index 個附件（不刪實際檔案）。"""
    d = await db.get(Defect, defect_id)
    if not d:
        raise HTTPException(404, "Defect not found")
    items = list(d.attachments_json or [])
    if index < 0 or index >= len(items):
        raise HTTPException(404, "Attachment index out of range")
    del items[index]
    d.attachments_json = items
    flag_modified(d, "attachments_json")
    await db.flush()
