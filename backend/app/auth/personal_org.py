"""Personal Organization helper(v1.1.11)。

每個 user 都應該有一個「個人 org」(slug=``personal-{username}``),user 在裡面
是 admin。設計理由:
* 被邀請到別人 org 的協作者,退出所有外部 project 後可以回到自己的工作空間
* SSO JIT 進來的新 user(``UserManager.get_or_provision_via_oidc``)以前沒建,
  造成他們卡在 Default Organization 看 read-only banner
* ``POST /auth/users`` admin 手動建出來的 user 也沒建

這支 helper 統一處理「找不到就建」的邏輯,callers:

* :meth:`UserManager.get_or_provision_via_oidc` — 新 SSO user
* :meth:`UserManager.on_after_login` — 既有 user lazy backfill(只跑一次)

註冊路徑(``POST /auth/register``)已經自己手刻建好,不需要呼這支(但呼了
也是 idempotent,只是浪費一次 SELECT)。
"""
from __future__ import annotations

import logging
import uuid
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.org_membership import OrgMembership
from app.models.organization import Organization
from app.models.role import Role
from app.models.user import User

logger = logging.getLogger(__name__)


def personal_org_slug(username: str) -> str:
    """個人 org 的 slug 規則 — 集中定義方便前端 / 後端對齊。"""
    return f"personal-{username.lower()}"


async def ensure_personal_org(
    db: AsyncSession,
    user: User,
    *,
    set_as_active: bool = False,
) -> Organization:
    """確保 ``user`` 有個人 organization + admin OrgMembership。

    回傳該個人 Organization。已存在 → 直接回(不重複建);不存在 → 建好整套
    (Organization + OrgMembership(admin, is_default=True))再回。

    Args:
        db: AsyncSession,呼叫者負責 commit / rollback。
        user: 目標 user(已 flush 進 DB,有 ``user.id`` / ``user.username``)。
        set_as_active: True → 順便把 ``user.organization_id`` / ``user.role_id``
            設成個人 org/admin(用於新 user 第一次建)。False → 只確保 org 存在,
            不動 user 當前 active org(用於既有 user lazy backfill,避免把他正在
            用的 org context 強制切走)。

    Side effects:
        * 可能 add Organization + OrgMembership 到 session
        * ``set_as_active=True`` 時改 user.organization_id / user.role_id
        * 不會 commit,由 caller 處理
    """
    slug = personal_org_slug(user.username)

    org = (
        await db.execute(select(Organization).where(Organization.slug == slug))
    ).scalar_one_or_none()

    if org is None:
        display = user.display_name or user.username
        org = Organization(
            id=str(uuid.uuid4()),
            slug=slug,
            name=f"{display} 的工作空間",
            plan="free",
        )
        db.add(org)
        await db.flush()
        logger.info(
            "personal_org: created org=%s slug=%s for user=%s",
            org.id, slug, user.username,
        )

    admin_role = (
        await db.execute(
            select(Role).where(Role.name == "admin", Role.is_system.is_(True))
        )
    ).scalar_one_or_none()
    admin_role_id = admin_role.id if admin_role else None

    mem = (
        await db.execute(
            select(OrgMembership)
            .where(OrgMembership.username == user.username)
            .where(OrgMembership.organization_id == org.id)
        )
    ).scalar_one_or_none()

    if mem is None:
        db.add(OrgMembership(
            username=user.username,
            user_id=user.id,
            organization_id=org.id,
            role_id=admin_role_id,
            is_default=True,
            status="active",
            invited_by=None,
        ))
        await db.flush()
        logger.info(
            "personal_org: created OrgMembership user=%s org=%s role=%s",
            user.username, org.id, admin_role_id,
        )
    else:
        # 既有 membership — 確保 status=active 且是 admin role
        changed = False
        if mem.status != "active":
            mem.status = "active"
            changed = True
        if admin_role_id and mem.role_id != admin_role_id:
            mem.role_id = admin_role_id
            changed = True
        if changed:
            await db.flush()

    if set_as_active:
        user.organization_id = org.id
        if admin_role_id:
            user.role_id = admin_role_id
        await db.flush()

    return org


async def user_has_personal_org(db: AsyncSession, user: User) -> bool:
    """純讀取版,給 callers 在不想跑 side effects 的時候判斷用。"""
    slug = personal_org_slug(user.username)
    row = (
        await db.execute(select(Organization.id).where(Organization.slug == slug))
    ).scalar_one_or_none()
    return row is not None


__all__ = ["personal_org_slug", "ensure_personal_org", "user_has_personal_org"]
