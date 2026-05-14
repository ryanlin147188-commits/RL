"""Generic review/approval workflow REST endpoints (RFC-Review-1).

Endpoints:
    POST   /api/reviews                         submit an entity for review
    GET    /api/reviews                         list (filter status / entity_type)
    GET    /api/reviews/{id}                    one record
    GET    /api/reviews/{id}/history            its full audit trail
    GET    /api/reviews/by-entity               look up by (entity_type, entity_id)
    POST   /api/reviews/{id}/approve            approve a pending review
    POST   /api/reviews/{id}/reject             reject (requires reason)
    POST   /api/reviews/{id}/revert             approved -> pending (requires reason)

Tenancy: ReviewRecord is TenantScoped — the auto-stamp ORM hook fills
``organization_id`` from the caller's JWT. Read endpoints use the same
filter so users only see their org's reviews.

Permissions: kept light for now -- any authenticated user can list and
submit; approve/reject/revert require Admin. Hook into RFC-5
``require_permission(...)`` here when role granularity matures.
"""
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.auth.tenant import TenantQuery
from app.database import get_db
from app.models.review import (
    ReviewableEntityType,
    ReviewHistory,
    ReviewRecord,
    ReviewStatus,
)
from app.models.user import User
from app.schemas.review import (
    RejectReviewRequest,
    RevertReviewRequest,
    ReviewHistoryEntry,
    ReviewRecordResponse,
    SubmitReviewRequest,
)
from app.services import review_service

router = APIRouter()


async def _ensure_admin(user: User, db: AsyncSession) -> None:
    """(legacy)`revert` 仍走純 Admin / superuser 的舊規則:revert 是把
    通過/退回的審核重新拉回 pending 的決策動作,跟 assignee 無關,
    交給 platform 管理員把關即可。"""
    if user.is_superuser:
        return
    if user.role_id is None:
        raise HTTPException(
            status_code=403,
            detail={"error": "permission_denied", "missing_permissions": ["review.manage"]},
        )
    from app.models.role import Role

    role = await db.get(Role, user.role_id)
    if role is None:
        raise HTTPException(403, "role not found")
    if role.name == "Admin":
        return
    if "review.manage" in (role.permissions_json or []):
        return
    raise HTTPException(
        status_code=403,
        detail={
            "error": "permission_denied",
            "missing_permissions": ["review.manage"],
        },
    )


async def _ensure_can_review(
    user: User, db: AsyncSession, record: ReviewRecord
) -> None:
    """approve / reject 的權限規則:
      1. superuser、Admin role:可覆蓋(平台管理員角色)
      2. 否則送審者本人不可自審
      3. 一般使用者必須具 `review.manage` 權限,且:
         - assignee_type='user' → username 等於 record.assigned_to
         - assignee_type='group' → 是該 group(含巢狀子群組)成員
    """
    if user.is_superuser:
        return

    from app.models.role import Role

    role = None
    if user.role_id is not None:
        role = await db.get(Role, user.role_id)
    role_perms = (role.permissions_json if role else None) or []
    is_platform_admin = role is not None and role.name == "Admin"

    # 1) Admin / superuser:覆蓋
    if is_platform_admin:
        return

    # 2) 自審防呆
    if record.submitted_by and record.submitted_by == user.username:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "self_review_forbidden",
                "message": "送審者本人不可審核此筆紀錄",
            },
        )

    # 3) 一般使用者:必須具 review.manage 才有資格進到 assignee 比對
    if "review.manage" not in role_perms:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "permission_denied",
                "missing_permissions": ["review.manage"],
            },
        )

    assigned_to = record.assigned_to or ""
    assigned_type = (record.assigned_to_type or "user").lower()
    if not assigned_to:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "no_assignee",
                "message": "此筆審核未指派審核者,請由 Admin 處理",
            },
        )

    if assigned_type == "user":
        if assigned_to != user.username:
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "not_assigned_reviewer",
                    "message": "您不是被指派的審核者",
                },
            )
        return

    if assigned_type == "group":
        from app.services.group_resolver import resolve_group_members

        members = await resolve_group_members(db, assigned_to)
        if user.username not in members:
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "not_in_assigned_group",
                    "message": "您不是被指派群組的成員",
                },
            )
        return

    raise HTTPException(
        status_code=400,
        detail=f"未知的 assignee_type: {assigned_type}",
    )


@router.post(
    "/reviews",
    response_model=ReviewRecordResponse,
    status_code=201,
    tags=["AB · 審核"],
)
async def submit_review(
    payload: SubmitReviewRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # 校驗 assignee 真實存在(避免送出髒資料)
    await _validate_assignee(db, payload.assignee, payload.assignee_type, user)
    record = await review_service.submit(
        db,
        entity_type=payload.entity_type,
        entity_id=payload.entity_id,
        submitted_by=user.username,
        organization_id=user.organization_id,
        assignee=payload.assignee,
        assignee_type=payload.assignee_type,
    )
    names = await _resolve_entity_names(db, [record])
    return _to_response(record, names)


async def _validate_assignee(
    db: AsyncSession, assignee: str, assignee_type: str, user: User
) -> None:
    """送審必選的 assignee 必須真的存在;user → users.username,group → groups.id。
    不在同 org 也擋(避免跨租戶指派)。"""
    if assignee_type == "user":
        target = (
            await db.execute(
                select(User).where(User.username == assignee)
            )
        ).scalar_one_or_none()
        if target is None:
            raise HTTPException(404, f"assignee user not found: {assignee}")
        if (
            not user.is_superuser
            and target.organization_id is not None
            and user.organization_id is not None
            and target.organization_id != user.organization_id
        ):
            raise HTTPException(403, "assignee not in your organization")
        return
    if assignee_type == "group":
        from app.models.group import Group

        target = await db.get(Group, assignee)
        if target is None:
            raise HTTPException(404, f"assignee group not found: {assignee}")
        if (
            not user.is_superuser
            and getattr(target, "organization_id", None) is not None
            and user.organization_id is not None
            and target.organization_id != user.organization_id
        ):
            raise HTTPException(403, "assignee group not in your organization")
        return
    raise HTTPException(422, f"invalid assignee_type: {assignee_type}")


async def _resolve_entity_names(
    db: AsyncSession, records: list[ReviewRecord]
) -> dict[tuple[str, str], str]:
    """Batch-resolve human-readable names for every (entity_type, entity_id)
    in `records`. Returns a flat {(type, id): name} map so the caller can
    just stitch into the response shape.

    Falls back to no-name (None) when the underlying entity was deleted --
    audit history must outlive the entity it audits.
    """
    from app.models.execution_report import ExecutionReport
    from app.models.recording import RecordingSession
    from app.models.tree_node import TreeNode

    by_type: dict[ReviewableEntityType, list[str]] = {}
    for r in records:
        by_type.setdefault(r.entity_type, []).append(r.entity_id)

    out: dict[tuple[str, str], str] = {}

    async def _fill(model, label_attr, etype: ReviewableEntityType):
        ids = by_type.get(etype) or []
        if not ids:
            return
        rows = (
            await db.execute(select(model).where(model.id.in_(ids)))
        ).scalars().all()
        for row in rows:
            label = getattr(row, label_attr, None)
            if label:
                out[(etype.value, row.id)] = str(label)

    await _fill(TreeNode, "name", ReviewableEntityType.TESTCASE)
    # RecordingSession does not have a name column; surface the target URL
    # so the operator at least sees what was being recorded.
    await _fill(RecordingSession, "target_url", ReviewableEntityType.SCRIPT)
    # ExecutionReport: surface the celery task_id (the trigger handle the
    # operator sees in /api/executions/{task_id}/status).
    await _fill(ExecutionReport, "task_id", ReviewableEntityType.REPORT)
    return out


def _to_response(record: ReviewRecord, names: dict[tuple[str, str], str]) -> ReviewRecordResponse:
    resp = ReviewRecordResponse.model_validate(record)
    resp.entity_name = names.get((record.entity_type.value, record.entity_id))
    return resp


@router.get(
    "/reviews",
    response_model=List[ReviewRecordResponse],
    tags=["AB · 審核"],
)
async def list_reviews(
    status: Optional[str] = Query(None),
    entity_type: Optional[ReviewableEntityType] = Query(None),
    mine: bool = Query(False, description="只看指派給我(含我所屬群組)的審核"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # 舊呼叫端(前端 / 第三方)仍會送 pending / approved / rejected,而 ReviewStatus
    # 已統一為 7 值 enum (commit 85ef91e)。在此手動接受舊別名,免得 Pydantic 422。
    resolved_status: Optional[ReviewStatus] = None
    if status is not None:
        legacy_alias = {"pending": "InReview", "approved": "Verified", "rejected": "Closed"}
        canonical = legacy_alias.get(status, status)
        try:
            resolved_status = ReviewStatus(canonical)
        except ValueError:
            allowed = ", ".join([e.value for e in ReviewStatus])
            raise HTTPException(422, f"Invalid status '{status}' (allowed: {allowed} or pending/approved/rejected)")
    stmt = TenantQuery.for_(ReviewRecord).order_by(ReviewRecord.updated_at.desc())
    if resolved_status is not None:
        stmt = stmt.where(ReviewRecord.status == resolved_status)
    if entity_type is not None:
        stmt = stmt.where(ReviewRecord.entity_type == entity_type)
    if mine:
        # 指派給我:assignee_type='user' AND assigned_to=username
        # OR assignee_type='group' AND assigned_to ∈ {我所在的全部群組(含巢狀)}
        from sqlalchemy import or_
        from app.models.group import GroupMembership

        my_group_ids = (
            await db.execute(
                select(GroupMembership.group_id).where(
                    GroupMembership.username == user.username
                )
            )
        ).scalars().all()
        clauses = [
            (ReviewRecord.assigned_to_type == "user")
            & (ReviewRecord.assigned_to == user.username),
        ]
        if my_group_ids:
            clauses.append(
                (ReviewRecord.assigned_to_type == "group")
                & (ReviewRecord.assigned_to.in_(list(my_group_ids)))
            )
        stmt = stmt.where(or_(*clauses))
    rows = (await db.execute(stmt)).scalars().all()
    names = await _resolve_entity_names(db, list(rows))
    # Drop orphans whose underlying entity has been deleted. Until v1.1
    # we showed "實體已刪除" placeholders; ops feedback (2026-04-30) said
    # that's noise. Cascade-delete in tree_service.recursive_delete
    # prevents NEW orphans; this filter sweeps any historical ones.
    return [
        _to_response(r, names)
        for r in rows
        if names.get((r.entity_type.value, r.entity_id)) is not None
    ]


@router.get(
    "/reviews/by-entity",
    response_model=Optional[ReviewRecordResponse],
    tags=["AB · 審核"],
)
async def get_review_by_entity(
    entity_type: ReviewableEntityType = Query(...),
    entity_id: str = Query(..., min_length=1),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Lookup the review state of a specific entity. Returns null if no
    review has ever been submitted for it."""
    record = await review_service.get_record(
        db,
        entity_type=entity_type,
        entity_id=entity_id,
        organization_id=None if user.is_superuser else user.organization_id,
    )
    if record is None:
        return None
    names = await _resolve_entity_names(db, [record])
    return _to_response(record, names)


@router.get(
    "/reviews/{record_id}",
    response_model=ReviewRecordResponse,
    tags=["AB · 審核"],
)
async def get_review(
    record_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    record = (
        await db.execute(TenantQuery.for_(ReviewRecord).where(ReviewRecord.id == record_id))
    ).scalar_one_or_none()
    if record is None:
        raise HTTPException(404, "review record not found")
    names = await _resolve_entity_names(db, [record])
    return _to_response(record, names)


@router.get(
    "/reviews/{record_id}/history",
    response_model=List[ReviewHistoryEntry],
    tags=["AB · 審核"],
)
async def get_review_history(
    record_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """先用 path 參數當 review_record.id 找;找不到就 fallback 當 entity_id 找
    (testcase / document 等業務 entity 的 ID),回傳對應 review 的 history。"""
    record = (
        await db.execute(TenantQuery.for_(ReviewRecord).where(ReviewRecord.id == record_id))
    ).scalar_one_or_none()
    if record is None:
        # 兼容呼叫端用業務 entity ID 直查的習慣 — 在 review_records 內找
        # entity_id == path 參數的 row(若有多筆則取最新一筆)
        record = (
            await db.execute(
                TenantQuery.for_(ReviewRecord)
                .where(ReviewRecord.entity_id == record_id)
                .order_by(ReviewRecord.created_at.desc())
            )
        ).scalars().first()
    if record is None:
        raise HTTPException(404, "review record not found")

    rows = (
        await db.execute(
            select(ReviewHistory)
            .where(ReviewHistory.review_record_id == record.id)
            .order_by(ReviewHistory.acted_at.asc())
        )
    ).scalars().all()
    return list(rows)


async def _load_for_action(
    db: AsyncSession, record_id: str, user: User
) -> ReviewRecord:
    record = (
        await db.execute(TenantQuery.for_(ReviewRecord).where(ReviewRecord.id == record_id))
    ).scalar_one_or_none()
    if record is None:
        raise HTTPException(404, "review record not found")
    return record


@router.post(
    "/reviews/{record_id}/approve",
    response_model=ReviewRecordResponse,
    tags=["AB · 審核"],
)
async def approve_review(
    record_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    record = await _load_for_action(db, record_id, user)
    await _ensure_can_review(user, db, record)
    record = await review_service.approve(db, record=record, reviewer=user.username)
    # AB 表 hook:對齊 entity_versions 的 content_status,並記一筆 source='system' 的 snapshot,
    # 讓「review 通過 = 進入 approved 版本」這件事在版本歷史上看得到。
    # 目前 review_records 僅涵蓋 testcase + document;其他 entity 透過
    # /api/entity-versions/{type}/{id}/approve 直接審核(見 entity_versions router)。
    await _sync_content_status_on_approve(db, record, user.username)
    names = await _resolve_entity_names(db, [record])
    return _to_response(record, names)


async def _sync_content_status_on_approve(db, record, username: str) -> None:
    """把 review record 對應的業務 entity 的 content_status flip 成 approved。

    review_records.entity_type 是大寫枚舉(TESTCASE / DOCUMENT / ...)。本函式
    把它 map 到 entity_versions registry 用的 lowercase key,並只處理 6 種已註冊
    的 entity。沒對到 → 靜默跳過(REPORT / SCRIPT 等非 AB 範圍的 entity 不影響)。
    """
    type_map = {
        "TESTCASE": "testcase",
    }
    entity_type_value = (
        record.entity_type.value if hasattr(record.entity_type, "value") else str(record.entity_type)
    )
    ev_type = type_map.get(entity_type_value.upper())
    if not ev_type:
        return
    from app.services import entity_version_service as evs
    spec = evs._get_registry().get(ev_type)
    if not spec:
        return
    entity = await db.get(spec.model, record.entity_id)
    if entity is None:
        return
    await evs.snapshot(
        db,
        entity_type=ev_type,
        entity=entity,
        source=evs.CHANGE_SOURCE_SYSTEM,
        status=evs.CONTENT_STATUS_APPROVED,
        by=username,
        reason="Approved via review_records",
    )


@router.post(
    "/reviews/{record_id}/reject",
    response_model=ReviewRecordResponse,
    tags=["AB · 審核"],
)
async def reject_review(
    record_id: str,
    payload: RejectReviewRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    record = await _load_for_action(db, record_id, user)
    await _ensure_can_review(user, db, record)
    record = await review_service.reject(
        db, record=record, reviewer=user.username, reason=payload.reason
    )
    await _sync_content_status_on_reject(db, record, user.username, payload.reason)
    names = await _resolve_entity_names(db, [record])
    return _to_response(record, names)


async def _sync_content_status_on_reject(db, record, username: str, reason: str | None) -> None:
    """同 _sync_content_status_on_approve,但 status 改 rejected。"""
    type_map = {"TESTCASE": "testcase"}
    entity_type_value = (
        record.entity_type.value if hasattr(record.entity_type, "value") else str(record.entity_type)
    )
    ev_type = type_map.get(entity_type_value.upper())
    if not ev_type:
        return
    from app.services import entity_version_service as evs
    spec = evs._get_registry().get(ev_type)
    if not spec:
        return
    entity = await db.get(spec.model, record.entity_id)
    if entity is None:
        return
    await evs.snapshot(
        db,
        entity_type=ev_type,
        entity=entity,
        source=evs.CHANGE_SOURCE_SYSTEM,
        status=evs.CONTENT_STATUS_REJECTED,
        by=username,
        reason=f"Rejected via review_records: {reason or '—'}",
    )


@router.post(
    "/reviews/{record_id}/revert",
    response_model=ReviewRecordResponse,
    tags=["AB · 審核"],
)
async def revert_review(
    record_id: str,
    payload: RevertReviewRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _ensure_admin(user, db)
    record = await _load_for_action(db, record_id, user)
    record = await review_service.revert(
        db, record=record, actor=user.username, reason=payload.reason
    )
    names = await _resolve_entity_names(db, [record])
    return _to_response(record, names)
