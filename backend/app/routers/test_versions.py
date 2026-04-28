"""TestVersion REST endpoints — 測試版號 CRUD。

org-scoped(同 settings/groups 模式);usage_count 統計反向引用數,
給前端「刪除前提示」用。
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import asc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.database import get_db
from app.models.defect import Defect
from app.models.execution_report import ExecutionReport
from app.models.test_round import TestRound
from app.models.test_version import TestVersion, VersionPlatform, VersionStatus
from app.models.user import User
from app.schemas.test_version import (
    TestVersionCreate,
    TestVersionResponse,
    TestVersionUpdate,
)

router = APIRouter()


def _scope(stmt, user: User):
    if user.is_superuser:
        return stmt
    return stmt.where(TestVersion.organization_id == user.organization_id)


def _resolve_platform(val: str) -> VersionPlatform:
    try:
        return VersionPlatform(val.upper())
    except (ValueError, AttributeError):
        raise HTTPException(400, "platform 必須是 WEB / API / APP")


def _resolve_status(val: str, default: VersionStatus = VersionStatus.RELEASED) -> VersionStatus:
    try:
        return VersionStatus(val)
    except ValueError:
        return default


async def _usage_count(db: AsyncSession, version_id: str) -> int:
    """統計反向引用:多少 ExecutionReport / Defect / TestRound 用到此版號。"""
    total = 0
    for model in (ExecutionReport, Defect, TestRound):
        # 直接 SQL count(test_version_id),避免 model attr 在某些 sub-class 不存在
        try:
            n = (await db.execute(
                select(func.count()).select_from(model)
                .where(getattr(model, "test_version_id") == version_id)
            )).scalar_one() or 0
            total += int(n)
        except Exception:
            # 欄位還沒 ALTER 上去(冷啟第一次)
            pass
    return total


def _to_response(v: TestVersion, usage_count: int = 0) -> dict:
    return {
        "id": v.id,
        "organization_id": v.organization_id,
        "project_id": v.project_id,
        "platform": v.platform.value if hasattr(v.platform, "value") else str(v.platform),
        "version_label": v.version_label,
        "description": v.description,
        "released_at": v.released_at,
        "status": v.status.value if hasattr(v.status, "value") else str(v.status),
        "created_by": v.created_by,
        "created_at": v.created_at,
        "updated_at": v.updated_at,
        "usage_count": usage_count,
    }


@router.get("/test-versions", response_model=list[TestVersionResponse], tags=["TV · 測試版號"])
async def list_test_versions(
    project_id: Optional[str] = Query(None),
    platform: Optional[str] = Query(None, description="WEB / API / APP"),
    status: Optional[str] = Query(None, description="planned / released / deprecated"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(TestVersion).order_by(
        asc(TestVersion.platform),
        asc(TestVersion.version_label),
    )
    stmt = _scope(stmt, user)
    if project_id:
        stmt = stmt.where(TestVersion.project_id == project_id)
    if platform:
        stmt = stmt.where(TestVersion.platform == _resolve_platform(platform))
    if status:
        stmt = stmt.where(TestVersion.status == _resolve_status(status))
    rows = (await db.execute(stmt)).scalars().all()
    out = []
    for v in rows:
        cnt = await _usage_count(db, v.id)
        out.append(_to_response(v, cnt))
    return out


@router.post(
    "/test-versions", response_model=TestVersionResponse, status_code=201, tags=["TV · 測試版號"]
)
async def create_test_version(
    payload: TestVersionCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    label = (payload.version_label or "").strip()
    if not label:
        raise HTTPException(400, "version_label 必填")
    plat = _resolve_platform(payload.platform)
    # 同 project + platform + label 唯一
    dup = (
        await db.execute(
            select(TestVersion).where(
                TestVersion.project_id == payload.project_id,
                TestVersion.platform == plat,
                TestVersion.version_label == label,
            )
        )
    ).scalar_one_or_none()
    if dup:
        raise HTTPException(
            409, f"版號 {plat.value} {label} 在此專案已存在"
        )
    v = TestVersion(
        organization_id=user.organization_id,
        project_id=payload.project_id,
        platform=plat,
        version_label=label,
        description=payload.description,
        released_at=payload.released_at,
        status=_resolve_status(payload.status),
        created_by=user.username,
    )
    db.add(v)
    await db.flush()
    await db.refresh(v)
    return _to_response(v, 0)


@router.put(
    "/test-versions/{version_id}",
    response_model=TestVersionResponse,
    tags=["TV · 測試版號"],
)
async def update_test_version(
    version_id: str,
    payload: TestVersionUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    v = await db.get(TestVersion, version_id)
    if not v:
        raise HTTPException(404, "TestVersion not found")
    if not user.is_superuser and v.organization_id != user.organization_id:
        raise HTTPException(404, "TestVersion not found")
    data = payload.model_dump(exclude_unset=True)

    new_platform = v.platform
    new_label = v.version_label
    if "platform" in data and data["platform"]:
        new_platform = _resolve_platform(data["platform"])
    if "version_label" in data and data["version_label"]:
        new_label = data["version_label"].strip()

    # 改名 / 改平台 → 重新檢查唯一
    if (new_platform != v.platform or new_label != v.version_label):
        dup = (
            await db.execute(
                select(TestVersion).where(
                    TestVersion.project_id == v.project_id,
                    TestVersion.platform == new_platform,
                    TestVersion.version_label == new_label,
                    TestVersion.id != v.id,
                )
            )
        ).scalar_one_or_none()
        if dup:
            raise HTTPException(409, f"版號 {new_platform.value} {new_label} 已存在")

    v.platform = new_platform
    v.version_label = new_label
    if "description" in data:
        v.description = data["description"]
    if "released_at" in data:
        v.released_at = data["released_at"]
    if "status" in data and data["status"]:
        v.status = _resolve_status(data["status"], v.status)

    await db.flush()
    await db.refresh(v)
    cnt = await _usage_count(db, v.id)
    return _to_response(v, cnt)


@router.delete("/test-versions/{version_id}", status_code=204, tags=["TV · 測試版號"])
async def delete_test_version(
    version_id: str,
    force: bool = Query(False, description="若有引用,加 force=1 強制刪除(引用方 set null)"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    v = await db.get(TestVersion, version_id)
    if not v:
        raise HTTPException(404, "TestVersion not found")
    if not user.is_superuser and v.organization_id != user.organization_id:
        raise HTTPException(404, "TestVersion not found")
    cnt = await _usage_count(db, v.id)
    if cnt > 0 and not force:
        raise HTTPException(
            409,
            f"還有 {cnt} 筆紀錄(報告/缺陷/回合)引用此版號;"
            f"確認要刪除請加 ?force=1(被引用方 test_version_id 會 set null)",
        )
    # FK 設為 ON DELETE SET NULL,直接 delete 即可;ALTER 時就帶 ON DELETE SET NULL
    await db.delete(v)
    await db.flush()
