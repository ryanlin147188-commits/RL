"""Group(團隊群組)REST endpoints — 設定頁的群組分頁。

設計:
- 任何登入者都能 list / 看自己 org 的群組;新建/改/刪 不限角色,
  RBAC 後續補上(memory `productization_gaps.md` 標 RBAC 執行為待補)。
- 巢狀:parent_id 自我 FK;移動 / 重設 parent 由 update endpoint 一次處理。
- 成員管理走 sub-resource:`/settings/groups/{id}/members`。
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import asc, delete as sa_delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.database import get_db
from app.models.group import Group, GroupMembership
from app.models.user import User
from app.schemas.group import (
    GroupAddMembersRequest,
    GroupCreate,
    GroupMemberResponse,
    GroupMemberUpdateRequest,
    GroupResponse,
    GroupUpdate,
)

router = APIRouter()


def _scope(stmt, user: User):
    """限制只看自己 org 的群組;superuser 看全部。"""
    if user.is_superuser:
        return stmt
    return stmt.where(Group.organization_id == user.organization_id)


async def _check_or_404(db: AsyncSession, group_id: str, user: User) -> Group:
    g = await db.get(Group, group_id)
    if not g:
        raise HTTPException(404, "Group not found")
    if not user.is_superuser and g.organization_id != user.organization_id:
        raise HTTPException(404, "Group not found")
    return g


async def _member_count_map(db: AsyncSession, group_ids: list[str]) -> dict[str, int]:
    if not group_ids:
        return {}
    rows = (
        await db.execute(
            select(GroupMembership.group_id, func.count(GroupMembership.username))
            .where(GroupMembership.group_id.in_(group_ids))
            .group_by(GroupMembership.group_id)
        )
    ).all()
    return {r[0]: int(r[1]) for r in rows}


def _to_response(g: Group, member_count: int = 0) -> dict:
    return {
        "id": g.id,
        "organization_id": g.organization_id,
        "name": g.name,
        "description": g.description,
        "group_type": g.group_type or "team",
        "parent_id": g.parent_id,
        "created_by": g.created_by,
        "created_at": g.created_at,
        "updated_at": g.updated_at,
        "member_count": member_count,
    }


# ─── Group CRUD ───────────────────────────────────────────────────────

@router.get("/settings/groups", response_model=list[GroupResponse], tags=["S · 設定"])
async def list_groups(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(Group).order_by(asc(Group.name))
    stmt = _scope(stmt, user)
    rows = (await db.execute(stmt)).scalars().all()
    counts = await _member_count_map(db, [g.id for g in rows])
    return [_to_response(g, counts.get(g.id, 0)) for g in rows]


@router.post(
    "/settings/groups",
    response_model=GroupResponse,
    status_code=201,
    tags=["S · 設定"],
)
async def create_group(
    payload: GroupCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    name = (payload.name or "").strip()
    if not name:
        raise HTTPException(400, "群組名稱必填")
    # 同 org 內 name 唯一(DB 也有 UniqueConstraint,這裡先擋以給友善訊息)
    dup = (
        await db.execute(
            select(Group).where(
                Group.name == name,
                Group.organization_id == user.organization_id,
            )
        )
    ).scalar_one_or_none()
    if dup:
        raise HTTPException(409, f"群組「{name}」在本組織內已存在")

    # 驗 parent_id:必須在同 org 內
    if payload.parent_id:
        parent = await _check_or_404(db, payload.parent_id, user)
        if parent.organization_id != user.organization_id and not user.is_superuser:
            raise HTTPException(400, "父群組不在本組織內")

    g = Group(
        organization_id=user.organization_id,
        name=name,
        description=payload.description,
        group_type=(payload.group_type or "team").strip() or "team",
        parent_id=payload.parent_id,
        created_by=user.username,
    )
    db.add(g)
    await db.flush()

    # 起始成員(去重 + 限同 org);建立者自動加為 owner
    member_users = set([user.username])
    for u in payload.initial_members or []:
        if u and u != user.username:
            member_users.add(u)

    # 驗使用者存在 + 同 org
    if member_users:
        users = (
            await db.execute(
                select(User).where(User.username.in_(member_users))
            )
        ).scalars().all()
        for u in users:
            if not user.is_superuser and u.organization_id != user.organization_id:
                continue
            db.add(GroupMembership(
                group_id=g.id,
                username=u.username,
                role_in_group="owner" if u.username == user.username else "member",
            ))
    await db.flush()
    await db.refresh(g)
    counts = await _member_count_map(db, [g.id])
    return _to_response(g, counts.get(g.id, 0))


@router.put(
    "/settings/groups/{group_id}",
    response_model=GroupResponse,
    tags=["S · 設定"],
)
async def update_group(
    group_id: str,
    payload: GroupUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    g = await _check_or_404(db, group_id, user)
    data = payload.model_dump(exclude_unset=True)

    if "name" in data and data["name"] is not None:
        new_name = data["name"].strip()
        if new_name and new_name != g.name:
            dup = (
                await db.execute(
                    select(Group).where(
                        Group.name == new_name,
                        Group.organization_id == g.organization_id,
                        Group.id != g.id,
                    )
                )
            ).scalar_one_or_none()
            if dup:
                raise HTTPException(409, f"群組「{new_name}」已存在")
            g.name = new_name

    if "description" in data:
        g.description = data["description"]
    if "group_type" in data and data["group_type"]:
        g.group_type = data["group_type"].strip() or "team"

    if "parent_id" in data:
        new_parent = data["parent_id"]
        # 不能設成自己,也不能形成循環(簡易遞迴檢查)
        if new_parent == g.id:
            raise HTTPException(400, "群組不能以自己為父")
        if new_parent:
            cur = await _check_or_404(db, new_parent, user)
            # 從候選父往上爬,若爬到自己就有循環
            seen = set()
            while cur and cur.id not in seen:
                if cur.id == g.id:
                    raise HTTPException(400, "巢狀循環:不能把群組移到自己的子群組下")
                seen.add(cur.id)
                if not cur.parent_id:
                    break
                cur = await db.get(Group, cur.parent_id)
        g.parent_id = new_parent

    await db.flush()
    await db.refresh(g)
    counts = await _member_count_map(db, [g.id])
    return _to_response(g, counts.get(g.id, 0))


@router.delete("/settings/groups/{group_id}", status_code=204, tags=["S · 設定"])
async def delete_group(
    group_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    g = await _check_or_404(db, group_id, user)
    # 子群組會因 ondelete=SET NULL 升頂(不連動刪除);成員 row 會 cascade 刪
    await db.delete(g)
    await db.flush()


# ─── Tier B3:群組使用數(讓 admin 看清刪除前的影響面) ─────────────
@router.get("/settings/groups/{group_id}/usage", tags=["S · 設定"])
async def get_group_usage(
    group_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """回傳群組使用情況:
    * `members_count` — 直屬成員數
    * `child_groups_count` — 直屬子群組數(刪除後它們會 SET NULL 升頂)
    * `linked_todos_count` — 多少 TodoItem 把 assignee_type='group' assignee=this
    * `linked_assignments_count` — 各 Assignable entity(Defect / TreeNode /
      Requirement / TestDocument / ReviewRecord)指派為此群組的數量
    """
    from app.models.todo_item import TodoItem
    from app.models.defect import Defect
    from app.models.requirement import Requirement
    from app.models.test_document import TestDocument
    from app.models.tree_node import TreeNode
    from app.models.review import ReviewRecord

    await _check_or_404(db, group_id, user)

    members_count = (await db.execute(
        select(func.count()).select_from(GroupMembership)
        .where(GroupMembership.group_id == group_id)
    )).scalar_one() or 0
    child_groups_count = (await db.execute(
        select(func.count()).select_from(Group).where(Group.parent_id == group_id)
    )).scalar_one() or 0

    # TodoItem 用 assignee_id (group_id 字串) + assignee_type='group'
    todos_count = (await db.execute(
        select(func.count()).select_from(TodoItem)
        .where(TodoItem.assignee == group_id)
        .where(TodoItem.assignee_type == "group")
    )).scalar_one() or 0

    # 其他 Assignable entity(都用 assigned_to + assigned_to_type)
    breakdown = {}
    for label, model in (
        ("defects", Defect),
        ("testcases", TreeNode),
        ("requirements", Requirement),
        ("documents", TestDocument),
        ("reviews", ReviewRecord),
    ):
        try:
            cnt = (await db.execute(
                select(func.count()).select_from(model)
                .where(model.assigned_to == group_id)
                .where(model.assigned_to_type == "group")
            )).scalar_one() or 0
            breakdown[label] = int(cnt)
        except Exception:
            breakdown[label] = 0

    linked_assignments_count = sum(breakdown.values())

    return {
        "group_id": group_id,
        "members_count": int(members_count),
        "child_groups_count": int(child_groups_count),
        "linked_todos_count": int(todos_count),
        "linked_assignments_count": linked_assignments_count,
        "breakdown": breakdown,
    }


# ─── Group Members ────────────────────────────────────────────────────

@router.get(
    "/settings/groups/{group_id}/members",
    response_model=list[GroupMemberResponse],
    tags=["S · 設定"],
)
async def list_members(
    group_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _check_or_404(db, group_id, user)
    rows = (
        await db.execute(
            select(GroupMembership, User)
            .join(User, User.username == GroupMembership.username)
            .where(GroupMembership.group_id == group_id)
            .order_by(asc(GroupMembership.role_in_group), asc(User.username))
        )
    ).all()
    return [
        {
            "group_id": m.group_id,
            "username": m.username,
            "role_in_group": m.role_in_group,
            "joined_at": m.joined_at,
            "display_name": u.display_name,
            "avatar_url": u.avatar_url,
        }
        for m, u in rows
    ]


@router.post(
    "/settings/groups/{group_id}/members",
    response_model=list[GroupMemberResponse],
    status_code=201,
    tags=["S · 設定"],
)
async def add_members(
    group_id: str,
    payload: GroupAddMembersRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    g = await _check_or_404(db, group_id, user)
    role = payload.role_in_group if payload.role_in_group in ("owner", "admin", "member") else "member"
    # 過濾同 org + 排除已存在者
    existing = set(
        (
            await db.execute(
                select(GroupMembership.username).where(GroupMembership.group_id == g.id)
            )
        ).scalars().all()
    )
    target_usernames = [u for u in (payload.usernames or []) if u and u not in existing]
    if not target_usernames:
        return []
    users = (
        await db.execute(
            select(User).where(User.username.in_(target_usernames))
        )
    ).scalars().all()
    added = []
    for u in users:
        if not user.is_superuser and u.organization_id != g.organization_id:
            continue
        gm = GroupMembership(
            group_id=g.id, username=u.username, role_in_group=role, joined_at=datetime.utcnow()
        )
        db.add(gm)
        added.append((gm, u))
    await db.flush()
    return [
        {
            "group_id": gm.group_id,
            "username": gm.username,
            "role_in_group": gm.role_in_group,
            "joined_at": gm.joined_at,
            "display_name": u.display_name,
            "avatar_url": u.avatar_url,
        }
        for gm, u in added
    ]


@router.put(
    "/settings/groups/{group_id}/members/{username}",
    response_model=GroupMemberResponse,
    tags=["S · 設定"],
)
async def update_member_role(
    group_id: str,
    username: str,
    payload: GroupMemberUpdateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _check_or_404(db, group_id, user)
    if payload.role_in_group not in ("owner", "admin", "member"):
        raise HTTPException(400, "role_in_group 必須是 owner / admin / member")
    gm = (
        await db.execute(
            select(GroupMembership).where(
                GroupMembership.group_id == group_id,
                GroupMembership.username == username,
            )
        )
    ).scalar_one_or_none()
    if not gm:
        raise HTTPException(404, "Member not found")
    gm.role_in_group = payload.role_in_group
    await db.flush()
    u = await db.get(User, username)
    return {
        "group_id": gm.group_id,
        "username": gm.username,
        "role_in_group": gm.role_in_group,
        "joined_at": gm.joined_at,
        "display_name": u.display_name if u else None,
        "avatar_url": u.avatar_url if u else None,
    }


@router.delete(
    "/settings/groups/{group_id}/members/{username}",
    status_code=204,
    tags=["S · 設定"],
)
async def remove_member(
    group_id: str,
    username: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _check_or_404(db, group_id, user)
    await db.execute(
        sa_delete(GroupMembership).where(
            GroupMembership.group_id == group_id,
            GroupMembership.username == username,
        )
    )
    await db.flush()
