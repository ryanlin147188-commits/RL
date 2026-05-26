"""SprintLink REST endpoints — Sprint(TestSchedule)跨實體連結 CRUD。

設計參照 [`todo_links.py`](todo_links.py),只是 owner 從 todo 改成 schedule。

支援 target_type:testcase / test_round / report / defect / todo(看板任務 = TodoItem)。
舊欄位 `test_schedules.linked_target_*` 維持讀(GET 時 stitch 進回傳列表並標 `is_legacy=True`),
新建/修改一律走 `sprint_links` 表;不再寫舊欄位。
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.database import get_db
from app.models.defect import Defect
from app.models.execution_report import ExecutionReport
from app.models.sprint_link import ALLOWED_TARGET_TYPES, SprintLink
from app.models.test_round import TestRound
from app.models.test_schedule import TestSchedule
from app.models.todo_item import TodoItem
from app.models.tree_node import LevelType, TreeNode
from app.models.user import User
from app.schemas.sprint_link import SprintLinkCreate, SprintLinkResponse

router = APIRouter()


# 各 target_type 對應的 (Model, title_attr, code_attr)
# testcase 走 TreeNode 特例(要驗 level_type == TESTCASE);其他直接 db.get
_TARGET_REGISTRY = {
    "test_round": (TestRound, "name", None),
    "report":     (ExecutionReport, "task_id", None),  # report 沒 title,task_id 當顯示
    "defect":     (Defect, "title", "code"),
    "todo":       (TodoItem, "title", None),
}


async def _validate_target(
    db: AsyncSession, target_type: str, target_id: str
) -> tuple[Optional[str], Optional[str]]:
    """驗證目標存在;回 (title, code)。失敗丟 HTTPException。"""
    if target_type not in ALLOWED_TARGET_TYPES:
        raise HTTPException(400, f"target_type 不支援:{target_type}")

    if target_type == "testcase":
        node = await db.get(TreeNode, target_id)
        if not node:
            raise HTTPException(404, f"找不到 testcase:{target_id}")
        if node.level_type != LevelType.TESTCASE:
            raise HTTPException(400, "target 必須是 level_type=TESTCASE 的 tree_node")
        return (node.name, None)

    spec = _TARGET_REGISTRY.get(target_type)
    if not spec:
        raise HTTPException(400, f"target_type 對應未實作:{target_type}")
    Model, title_attr, code_attr = spec
    obj = await db.get(Model, target_id)
    if not obj:
        raise HTTPException(404, f"找不到 {target_type}:{target_id}")
    title = getattr(obj, title_attr, None)
    code = getattr(obj, code_attr, None) if code_attr else None
    if hasattr(title, "value"):
        title = title.value
    if hasattr(code, "value"):
        code = code.value
    return (title, code)


async def _enrich_link(db: AsyncSession, link: SprintLink) -> dict:
    """把 SprintLink ORM row 跟 target 的 title/code 一起組成 response dict。
    target 不存在(被刪)時退而求其次顯示 placeholder,不擋 list endpoint。"""
    try:
        title, code = await _validate_target(db, link.target_type, link.target_id)
    except HTTPException:
        title, code = "(target 已不存在)", None
    return {
        "id": link.id,
        "schedule_id": link.schedule_id,
        "organization_id": link.organization_id,
        "target_type": link.target_type,
        "target_id": link.target_id,
        "link_kind": link.link_kind,
        "note": link.note,
        "created_at": link.created_at,
        "created_by": link.created_by,
        "target_title": title,
        "target_code": code,
        "is_legacy": False,
    }


async def _legacy_row_for_schedule(db: AsyncSession, schedule: TestSchedule) -> Optional[dict]:
    """若 schedule 有舊欄位 linked_target_type/id → 組成 virtual link dict(is_legacy=True)。"""
    t, tid = (schedule.linked_target_type or None), (schedule.linked_target_id or None)
    if not t or not tid:
        return None
    # 舊欄位有 project 這個 type,新表沒列在 ALLOWED — 仍允許顯示,但 type 用原值。
    try:
        if t == "project":
            from app.models.project import Project
            obj = await db.get(Project, tid)
            title = obj.name if obj else "(已刪除的專案)"
            code = None
        else:
            title, code = await _validate_target(db, t, tid)
    except HTTPException:
        title, code = "(target 已不存在)", None
    return {
        "id": f"legacy-{schedule.id}",
        "schedule_id": schedule.id,
        "organization_id": schedule.organization_id,
        "target_type": t,
        "target_id": tid,
        "link_kind": "relates_to",
        "note": None,
        "created_at": None,
        "created_by": None,
        "target_title": title,
        "target_code": code,
        "is_legacy": True,
    }


# ── 1) 列出某 Sprint 的 outbound links(含 legacy 單一連結)──────────────
@router.get(
    "/test-schedules/{schedule_id}/links",
    response_model=list[SprintLinkResponse],
    tags=["S · Sprint"],
)
async def list_sprint_links(
    schedule_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    schedule = await db.get(TestSchedule, schedule_id)
    if not schedule:
        raise HTTPException(404, "Sprint not found")
    if not user.is_superuser and schedule.organization_id != user.organization_id:
        raise HTTPException(404, "Sprint not found")

    rows = (
        await db.execute(
            select(SprintLink)
            .where(SprintLink.schedule_id == schedule_id)
            .order_by(SprintLink.created_at)
        )
    ).scalars().all()
    out = [await _enrich_link(db, l) for l in rows]
    legacy = await _legacy_row_for_schedule(db, schedule)
    if legacy:
        out.insert(0, legacy)
    return out


# ── 2) 新增連結 ────────────────────────────────────────────────────────
@router.post(
    "/test-schedules/{schedule_id}/links",
    response_model=SprintLinkResponse,
    status_code=201,
    tags=["S · Sprint"],
)
async def create_sprint_link(
    schedule_id: str,
    payload: SprintLinkCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    schedule = await db.get(TestSchedule, schedule_id)
    if not schedule:
        raise HTTPException(404, "Sprint not found")
    if not user.is_superuser and schedule.organization_id != user.organization_id:
        raise HTTPException(404, "Sprint not found")

    # 驗證目標存在
    await _validate_target(db, payload.target_type, payload.target_id)

    # 重複檢查 (UniqueConstraint 也有擋,先檢查給更友善的錯誤)
    dup = (
        await db.execute(
            select(SprintLink).where(
                SprintLink.schedule_id == schedule_id,
                SprintLink.target_type == payload.target_type,
                SprintLink.target_id == payload.target_id,
                SprintLink.link_kind == (payload.link_kind or "relates_to"),
            )
        )
    ).scalar_one_or_none()
    if dup:
        raise HTTPException(409, "Link already exists")

    link = SprintLink(
        organization_id=user.organization_id or schedule.organization_id,
        schedule_id=schedule_id,
        target_type=payload.target_type,
        target_id=payload.target_id,
        link_kind=payload.link_kind or "relates_to",
        note=payload.note,
        created_by=user.username,
    )
    db.add(link)
    await db.flush()
    await db.refresh(link)
    return await _enrich_link(db, link)


# ── 3) 刪除連結 ────────────────────────────────────────────────────────
@router.delete(
    "/test-schedules/links/{link_id}",
    status_code=204,
    tags=["S · Sprint"],
)
async def delete_sprint_link(
    link_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # legacy 連結是虛擬 row,id 形如 "legacy-<sid>" — 不允許用此端點刪
    # (要清掉 legacy 連結請改用 PATCH /test-schedules/{id} 把 linked_target_* 設 null)
    if link_id.startswith("legacy-"):
        raise HTTPException(
            400,
            "舊欄位連結不可由此端點刪除,請改用 PATCH /test-schedules/{id} 清掉 linked_target_*",
        )
    link = await db.get(SprintLink, link_id)
    if not link:
        raise HTTPException(404, "Link not found")
    if not user.is_superuser and link.organization_id != user.organization_id:
        raise HTTPException(404, "Link not found")
    await db.delete(link)
    await db.flush()
    return None
