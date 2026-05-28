"""Defect (缺陷管理) REST endpoints。

Endpoints:
    POST   /api/defects                       建立(testcase.write)
    GET    /api/defects                       list + filter(project 成員)
    GET    /api/defects/{id}                  detail(project 成員)
    PATCH  /api/defects/{id}                  update / status transition(testcase.write)
    DELETE /api/defects/{id}                  刪除(testcase.delete 或 Admin)
    POST   /api/defects/from-report           從失敗 ExecutionReport 一鍵建(testcase.write)
    POST   /api/defects/{id}/attachments      多檔上傳到 SeaweedFS pic bucket
    DELETE /api/defects/{id}/attachments/{key} 移除一筆附件

Tenancy: Defect 已繼承 TenantScoped,read 用 TenantQuery 自動 org-scoped。
Code 自動編號: ``DEF-`` + 該 project 內 Defect 數量+1 zero-padded 5 位。
Status transition: router 端 enforce(NEW→ASSIGNED/IN_PROGRESS/CLOSED 等),非法跳躍 422。
"""
from __future__ import annotations

import io
import uuid
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.auth.permissions import require_permission
from app.auth.permissions_catalog import P
from app.auth.tenant import TenantQuery
from app.database import get_db
from app.models.defect import Defect, DefectPriority, DefectSeverity, DefectStatus
from app.models.execution_report import ExecutionReport
from app.models.execution_step_log import ExecutionStepLog
from app.models.tree_node import TreeNode
from app.models.user import User
from app.schemas.defect import (
    DefectCreate,
    DefectFromReportRequest,
    DefectLinkedRef,
    DefectResponse,
    DefectUpdate,
)
from app.services import defect_service

router = APIRouter()


# ── Status transition 規則 ─────────────────────────────────────────
# 用 dict[from] = allowed_to_set 表達;router 在 PATCH 時驗證。
# Closed → Assigned 是「re-open」走 ASSIGNED;VERIFIED 是「QA 驗證過修復」。
_ALLOWED_TRANSITIONS: dict[DefectStatus, set[DefectStatus]] = {
    DefectStatus.NEW: {DefectStatus.ASSIGNED, DefectStatus.IN_PROGRESS, DefectStatus.CLOSED},
    DefectStatus.ASSIGNED: {DefectStatus.IN_PROGRESS, DefectStatus.CLOSED},
    DefectStatus.IN_PROGRESS: {DefectStatus.IN_REVIEW, DefectStatus.CLOSED},
    DefectStatus.IN_REVIEW: {DefectStatus.REWORK_REQUIRED, DefectStatus.VERIFIED, DefectStatus.CLOSED},
    DefectStatus.REWORK_REQUIRED: {DefectStatus.IN_PROGRESS, DefectStatus.CLOSED},
    DefectStatus.VERIFIED: {DefectStatus.CLOSED},
    DefectStatus.CLOSED: {DefectStatus.ASSIGNED},  # re-open
}


def _validate_transition(current: DefectStatus, target: DefectStatus) -> None:
    if current == target:
        return  # PATCH 帶同樣 status = no-op,放行
    allowed = _ALLOWED_TRANSITIONS.get(current, set())
    if target not in allowed:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "invalid_status_transition",
                "message": f"無法從 {current.value} 直接跳到 {target.value}",
                "allowed": [s.value for s in allowed],
            },
        )


async def _next_code(db: AsyncSession, project_id: str) -> str:
    """生 `DEF-00001` 風格的編號(per-project)。

    用 COUNT(*) + 1。極端併發下可能撞號,但 defect 建立頻率低、無 unique
    constraint,撞號的後果是「兩筆同 code」— 不會 crash,僅 UI 視覺重複。
    若日後要嚴格,改成 DB sequence 或 advisory lock。
    """
    n = await db.scalar(
        select(func.count(Defect.id)).where(Defect.project_id == project_id)
    )
    return f"DEF-{(n or 0) + 1:05d}"


async def _batch_resolve_links(
    db: AsyncSession, defects: list[Defect]
) -> tuple[dict[str, DefectLinkedRef], dict[str, DefectLinkedRef]]:
    """批次 resolve linked_testcase / linked_report 名稱,避免 N+1。"""
    testcase_ids = {d.linked_testcase_id for d in defects if d.linked_testcase_id}
    report_ids = {d.linked_report_id for d in defects if d.linked_report_id}

    tc_map: dict[str, DefectLinkedRef] = {}
    if testcase_ids:
        rows = (
            await db.execute(select(TreeNode).where(TreeNode.id.in_(testcase_ids)))
        ).scalars().all()
        for row in rows:
            tc_map[row.id] = DefectLinkedRef(id=row.id, name=row.name)

    rp_map: dict[str, DefectLinkedRef] = {}
    if report_ids:
        rows = (
            await db.execute(select(ExecutionReport).where(ExecutionReport.id.in_(report_ids)))
        ).scalars().all()
        for row in rows:
            rp_map[row.id] = DefectLinkedRef(
                id=row.id,
                name=row.task_id or row.id[:8],
                status=row.status.value if row.status else None,
            )
    return tc_map, rp_map


def _sign_attachments(attachments: Optional[list[dict]]) -> Optional[list[dict]]:
    """把 attachments_json 內每筆 ``/pics/...`` 或 ``/results/...`` URL 簽上
    短期 artifact_token。``<img src>`` / ``<video src>`` 沒辦法帶 Authorization
    header,只能靠 query string 內的 token 過 ``_authorize_artifact``。
    參考 reports.py 對 ExecutionStepLog 的處理。
    """
    if not attachments:
        return attachments
    from app.services.artifact_urls import sign_artifact_url

    out: list[dict] = []
    for a in attachments:
        if not isinstance(a, dict):
            continue
        new_a = dict(a)
        if "url" in new_a:
            signed = sign_artifact_url(new_a.get("url"))
            if signed:
                new_a["url"] = signed
        out.append(new_a)
    return out


def _to_response(
    d: Defect,
    tc_map: Optional[dict[str, DefectLinkedRef]] = None,
    rp_map: Optional[dict[str, DefectLinkedRef]] = None,
) -> DefectResponse:
    resp = DefectResponse.model_validate(d)
    if tc_map and d.linked_testcase_id:
        resp.linked_testcase = tc_map.get(d.linked_testcase_id)
    if rp_map and d.linked_report_id:
        resp.linked_report = rp_map.get(d.linked_report_id)
    # v1.1.9 fix:把 attachments 內的 url 簽過,讓前端 <img src> 載得起來
    resp.attachments_json = _sign_attachments(resp.attachments_json)
    return resp


# ── Endpoints ─────────────────────────────────────────────────────


@router.post(
    "/defects",
    response_model=DefectResponse,
    status_code=201,
    tags=["AC · 缺陷管理"],
)
async def create_defect(
    payload: DefectCreate,
    user: User = Depends(require_permission(P.TESTCASE_WRITE)),
    db: AsyncSession = Depends(get_db),
):
    code = await _next_code(db, payload.project_id)
    defect = Defect(
        id=str(uuid.uuid4()),
        project_id=payload.project_id,
        code=code,
        title=payload.title,
        description=payload.description,
        steps_to_reproduce=payload.steps_to_reproduce,
        expected_result=payload.expected_result,
        actual_result=payload.actual_result,
        severity=payload.severity,
        priority=payload.priority,
        status=DefectStatus.ASSIGNED if payload.assignee else DefectStatus.NEW,
        reporter=user.username,
        assignee=payload.assignee,
        linked_testcase_id=payload.linked_testcase_id,
        linked_report_id=payload.linked_report_id,
        test_version_id=payload.test_version_id,
        attachments_json=[],
    )
    db.add(defect)
    await db.commit()
    await db.refresh(defect)
    tc_map, rp_map = await _batch_resolve_links(db, [defect])
    return _to_response(defect, tc_map, rp_map)


@router.get(
    "/defects",
    response_model=List[DefectResponse],
    tags=["AC · 缺陷管理"],
)
async def list_defects(
    project_id: Optional[str] = Query(None),
    status: Optional[DefectStatus] = Query(None),
    severity: Optional[DefectSeverity] = Query(None),
    priority: Optional[DefectPriority] = Query(None),
    assignee: Optional[str] = Query(None, description="username;設成 'null' 找未指派"),
    search: Optional[str] = Query(None, description="search code / title 子字串"),
    open_only: bool = Query(False, description="排除 CLOSED 跟 VERIFIED"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    stmt = TenantQuery.for_(Defect).order_by(Defect.created_at.desc())
    if project_id:
        stmt = stmt.where(Defect.project_id == project_id)
    if status:
        stmt = stmt.where(Defect.status == status)
    if severity:
        stmt = stmt.where(Defect.severity == severity)
    if priority:
        stmt = stmt.where(Defect.priority == priority)
    if assignee is not None:
        if assignee == "null":
            stmt = stmt.where(Defect.assignee.is_(None))
        else:
            stmt = stmt.where(Defect.assignee == assignee)
    if search:
        like = f"%{search}%"
        stmt = stmt.where((Defect.code.ilike(like)) | (Defect.title.ilike(like)))
    if open_only:
        stmt = stmt.where(Defect.status.notin_([DefectStatus.CLOSED, DefectStatus.VERIFIED]))

    stmt = stmt.offset((page - 1) * page_size).limit(page_size)
    rows = (await db.execute(stmt)).scalars().all()
    tc_map, rp_map = await _batch_resolve_links(db, list(rows))
    return [_to_response(d, tc_map, rp_map) for d in rows]


@router.get(
    "/defects/count",
    tags=["AC · 缺陷管理"],
)
async def count_defects(
    project_id: Optional[str] = Query(None),
    status: Optional[DefectStatus] = Query(None),
    assignee: Optional[str] = Query(None),
    open_only: bool = Query(False),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, int]:
    """nav badge 用:回傳符合條件的 defect 數量(輕量,不抓 row body)。"""
    stmt = select(func.count(Defect.id))
    if project_id:
        stmt = stmt.where(Defect.project_id == project_id)
    if status:
        stmt = stmt.where(Defect.status == status)
    if assignee is not None:
        if assignee == "null":
            stmt = stmt.where(Defect.assignee.is_(None))
        else:
            stmt = stmt.where(Defect.assignee == assignee)
    if open_only:
        stmt = stmt.where(Defect.status.notin_([DefectStatus.CLOSED, DefectStatus.VERIFIED]))
    n = await db.scalar(stmt)
    return {"count": int(n or 0)}


@router.get(
    "/defects/{defect_id}",
    response_model=DefectResponse,
    tags=["AC · 缺陷管理"],
)
async def get_defect(
    defect_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    defect = (
        await db.execute(TenantQuery.for_(Defect).where(Defect.id == defect_id))
    ).scalar_one_or_none()
    if defect is None:
        raise HTTPException(404, "Defect not found")
    tc_map, rp_map = await _batch_resolve_links(db, [defect])
    return _to_response(defect, tc_map, rp_map)


@router.patch(
    "/defects/{defect_id}",
    response_model=DefectResponse,
    tags=["AC · 缺陷管理"],
)
async def update_defect(
    defect_id: str,
    payload: DefectUpdate,
    user: User = Depends(require_permission(P.TESTCASE_WRITE)),
    db: AsyncSession = Depends(get_db),
):
    defect = (
        await db.execute(TenantQuery.for_(Defect).where(Defect.id == defect_id))
    ).scalar_one_or_none()
    if defect is None:
        raise HTTPException(404, "Defect not found")

    data = payload.model_dump(exclude_unset=True)
    if "status" in data and data["status"] is not None:
        new_status = DefectStatus(data["status"]) if not isinstance(data["status"], DefectStatus) else data["status"]
        _validate_transition(defect.status, new_status)
        if new_status == DefectStatus.CLOSED and defect.closed_at is None:
            defect.closed_at = datetime.utcnow()
        if defect.status == DefectStatus.CLOSED and new_status != DefectStatus.CLOSED:
            defect.closed_at = None  # re-open
        defect.status = new_status
        data.pop("status")

    for k, v in data.items():
        setattr(defect, k, v)
    defect.updated_at = datetime.utcnow()
    await db.commit()
    await db.refresh(defect)
    tc_map, rp_map = await _batch_resolve_links(db, [defect])
    return _to_response(defect, tc_map, rp_map)


@router.delete(
    "/defects/{defect_id}",
    status_code=204,
    tags=["AC · 缺陷管理"],
)
async def delete_defect(
    defect_id: str,
    user: User = Depends(require_permission(P.TESTCASE_DELETE)),
    db: AsyncSession = Depends(get_db),
):
    defect = (
        await db.execute(TenantQuery.for_(Defect).where(Defect.id == defect_id))
    ).scalar_one_or_none()
    if defect is None:
        raise HTTPException(404, "Defect not found")
    await defect_service.hard_delete(db, defect)
    await db.commit()


@router.post(
    "/defects/from-report",
    response_model=DefectResponse,
    status_code=201,
    tags=["AC · 缺陷管理"],
)
async def create_defect_from_report(
    payload: DefectFromReportRequest,
    user: User = Depends(require_permission(P.TESTCASE_WRITE)),
    db: AsyncSession = Depends(get_db),
):
    report = await db.get(ExecutionReport, payload.report_id)
    if report is None:
        raise HTTPException(404, f"Report not found: {payload.report_id}")

    step: Optional[ExecutionStepLog] = None
    if payload.step_id:
        step = await db.get(ExecutionStepLog, payload.step_id)
        if step is None or step.report_id != report.id:
            raise HTTPException(404, f"Step not found in report: {payload.step_id}")

    # 從 step 反推 testcase / actual_result / screenshot
    linked_testcase_id: Optional[str] = None
    actual_result: Optional[str] = None
    attachments: list[dict[str, object]] = []
    if step is not None:
        linked_testcase_id = step.testcase_node_id
        actual_result = step.error_message
        for url, label in (
            (step.post_screenshot_url, "post_screenshot"),
            (step.pre_screenshot_url, "pre_screenshot"),
            (step.screenshot_diff_url, "screenshot_diff"),
            (step.video_url, "video"),
        ):
            if url:
                attachments.append({
                    "name": f"{label}.{'mp4' if 'video' in label else 'png'}",
                    "url": url,
                    "type": "video/mp4" if "video" in label else "image/png",
                })

    # title 預填
    tc_name = None
    if linked_testcase_id:
        node = await db.get(TreeNode, linked_testcase_id)
        if node is not None:
            tc_name = node.name
    auto_title = payload.title_override or (
        f"[失敗] {tc_name or '未知案例'}"
        + (f" / {(actual_result or '')[:60]}" if actual_result else "")
    )

    code = await _next_code(db, report.project_id)
    defect = Defect(
        id=str(uuid.uuid4()),
        project_id=report.project_id,
        code=code,
        title=auto_title,
        actual_result=actual_result,
        severity=payload.severity,
        priority=payload.priority,
        status=DefectStatus.ASSIGNED if payload.assignee else DefectStatus.NEW,
        reporter=user.username,
        assignee=payload.assignee,
        linked_testcase_id=linked_testcase_id,
        linked_report_id=report.id,
        test_version_id=report.test_version_id,
        attachments_json=attachments,
    )
    db.add(defect)
    await db.commit()
    await db.refresh(defect)
    tc_map, rp_map = await _batch_resolve_links(db, [defect])
    return _to_response(defect, tc_map, rp_map)


@router.post(
    "/defects/{defect_id}/attachments",
    response_model=DefectResponse,
    tags=["AC · 缺陷管理"],
)
async def upload_attachments(
    defect_id: str,
    files: list[UploadFile] = File(...),
    user: User = Depends(require_permission(P.TESTCASE_WRITE)),
    db: AsyncSession = Depends(get_db),
):
    """多檔上傳到 SeaweedFS pic bucket,append 進 attachments_json。"""
    defect = (
        await db.execute(TenantQuery.for_(Defect).where(Defect.id == defect_id))
    ).scalar_one_or_none()
    if defect is None:
        raise HTTPException(404, "Defect not found")

    from app.services.storage_service import _backend, _S3Storage

    if not isinstance(_backend, _S3Storage):
        raise HTTPException(500, "S3 storage backend not available")

    attachments = list(defect.attachments_json or [])
    for f in files:
        content = await f.read()
        ext = ""
        if f.filename and "." in f.filename:
            ext = "." + f.filename.rsplit(".", 1)[-1]
        key = f"defects/{defect_id}/{uuid.uuid4().hex}{ext}"
        _backend._client.put_object(
            Bucket="pic",
            Key=key,
            Body=io.BytesIO(content),
            ContentType=f.content_type or "application/octet-stream",
        )
        attachments.append({
            "name": f.filename or key.rsplit("/", 1)[-1],
            "url": f"/pics/{key}",
            "size": len(content),
            "type": f.content_type or "application/octet-stream",
        })
    defect.attachments_json = attachments
    defect.updated_at = datetime.utcnow()
    await db.commit()
    await db.refresh(defect)
    tc_map, rp_map = await _batch_resolve_links(db, [defect])
    return _to_response(defect, tc_map, rp_map)


@router.delete(
    "/defects/{defect_id}/attachments",
    response_model=DefectResponse,
    tags=["AC · 缺陷管理"],
)
async def remove_attachment(
    defect_id: str,
    url: str = Query(..., description="附件 URL(可帶或不帶 ?artifact_token)"),
    user: User = Depends(require_permission(P.TESTCASE_WRITE)),
    db: AsyncSession = Depends(get_db),
):
    defect = (
        await db.execute(TenantQuery.for_(Defect).where(Defect.id == defect_id))
    ).scalar_one_or_none()
    if defect is None:
        raise HTTPException(404, "Defect not found")
    # 前端拿到的 url 已被 _sign_attachments 加上 ?artifact_token=...,
    # 但 attachments_json 內存的是 raw URL。比對前先把 query string 去掉
    # 再 match path。
    from urllib.parse import urlsplit

    target_path = urlsplit(url).path or url
    attachments = [
        a for a in (defect.attachments_json or [])
        if (urlsplit(a.get("url") or "").path or a.get("url")) != target_path
    ]
    if len(attachments) == len(defect.attachments_json or []):
        raise HTTPException(404, f"Attachment not found: {url}")
    defect.attachments_json = attachments
    defect.updated_at = datetime.utcnow()
    await db.commit()
    await db.refresh(defect)
    tc_map, rp_map = await _batch_resolve_links(db, [defect])
    return _to_response(defect, tc_map, rp_map)
