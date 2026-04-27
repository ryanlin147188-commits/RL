"""Organization REST endpoints — superuser-only(部分端點開放給組織內 admin)。"""
from __future__ import annotations

import re
import secrets
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy import asc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.database import get_db
from app.models.org_invite import OrgInvite
from app.models.organization import Organization
from app.models.user import User

router = APIRouter()


class OrgCreate(BaseModel):
    slug: str
    name: str
    description: Optional[str] = None
    plan: Optional[str] = "free"
    email_domains: Optional[str] = None


class OrgUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    plan: Optional[str] = None
    email_domains: Optional[str] = None


class OrgResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    slug: str
    name: str
    description: Optional[str] = None
    plan: Optional[str] = None
    email_domains: Optional[str] = None


class InviteCreate(BaseModel):
    organization_id: Optional[str] = None  # 不傳 = 用呼叫者所屬 org
    email: Optional[str] = None
    role_id: Optional[str] = None
    group_id: Optional[str] = None
    note: Optional[str] = None
    expires_in_days: int = 7  # 預設 7 天過期


class InviteResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    token: str
    organization_id: str
    email: Optional[str] = None
    role_id: Optional[str] = None
    group_id: Optional[str] = None
    note: Optional[str] = None
    expires_at: Optional[datetime] = None
    used_by: Optional[str] = None
    used_at: Optional[datetime] = None
    created_by: Optional[str] = None
    created_at: datetime
    # 額外計算
    is_used: bool = False
    is_expired: bool = False


def _require_superuser(user: User) -> None:
    if not user.is_superuser:
        raise HTTPException(403, "需要 superuser 權限")


SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,58}$")
DOMAIN_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$")


def _normalize_domains(raw: Optional[str]) -> Optional[str]:
    """把使用者填的 email_domains 字串正規化:小寫、去空白、去 @ 前綴、驗格式。

    輸入格式範例:`Acme.com, @acme.io ; sub.example.org`
    輸出:`acme.com,acme.io,sub.example.org` 或 None(空)。
    格式錯誤的 domain 會被丟掉(不擋寫入,以利使用者快速試)。
    """
    if not raw:
        return None
    parts = re.split(r"[,;\s]+", raw.strip())
    cleaned = []
    seen = set()
    for p in parts:
        d = p.strip().lower().lstrip("@")
        if not d or d in seen:
            continue
        if not DOMAIN_RE.match(d):
            continue
        seen.add(d)
        cleaned.append(d)
    return ",".join(cleaned) if cleaned else None


def _match_org_by_email(email: str, orgs: list[Organization]) -> Optional[Organization]:
    """從 email 後綴找對應 org;沒 match 回 None。"""
    if not email or "@" not in email:
        return None
    domain = email.split("@", 1)[1].strip().lower()
    if not domain:
        return None
    for org in orgs:
        if not org.email_domains:
            continue
        for d in org.email_domains.split(","):
            if d.strip().lower() == domain:
                return org
    return None


@router.get("/organizations", response_model=list[OrgResponse], tags=["X · 組織"])
async def list_orgs(
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    _require_superuser(user)
    rows = (await db.execute(select(Organization).order_by(Organization.slug))).scalars().all()
    return list(rows)


@router.get("/organizations/me", response_model=OrgResponse, tags=["X · 組織"])
async def my_org(
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    """任何登入者都能拿到自己所屬 organization 的資訊(用於設定頁顯示 email_domains)。
    寫入仍走 PUT /organizations/{id},仍受 superuser 限制。"""
    if not user.organization_id:
        raise HTTPException(404, "尚未掛到任何組織")
    org = await db.get(Organization, user.organization_id)
    if not org:
        raise HTTPException(404, "Organization not found")
    return org


@router.post(
    "/organizations", response_model=OrgResponse, status_code=201, tags=["X · 組織"]
)
async def create_org(
    payload: OrgCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _require_superuser(user)
    if not SLUG_RE.match(payload.slug or ""):
        raise HTTPException(400, "slug 格式錯誤（小寫英數字底線連字號，2-59 字元）")
    existing = (
        await db.execute(select(Organization).where(Organization.slug == payload.slug))
    ).scalar_one_or_none()
    if existing:
        raise HTTPException(409, f"slug「{payload.slug}」已存在")
    org = Organization(
        slug=payload.slug,
        name=payload.name,
        description=payload.description,
        plan=payload.plan or "free",
        email_domains=_normalize_domains(payload.email_domains),
    )
    db.add(org)
    await db.flush()
    await db.refresh(org)
    return org


@router.put("/organizations/{org_id}", response_model=OrgResponse, tags=["X · 組織"])
async def update_org(
    org_id: str,
    payload: OrgUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _require_superuser(user)
    org = await db.get(Organization, org_id)
    if not org:
        raise HTTPException(404, "Organization not found")
    data = payload.model_dump(exclude_unset=True)
    for k, v in data.items():
        if k == "email_domains":
            org.email_domains = _normalize_domains(v)
        elif v is not None:
            setattr(org, k, v)
    await db.flush()
    await db.refresh(org)
    return org


@router.delete("/organizations/{org_id}", status_code=204, tags=["X · 組織"])
async def delete_org(
    org_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _require_superuser(user)
    org = await db.get(Organization, org_id)
    if not org:
        raise HTTPException(404, "Organization not found")
    if org.slug == "default":
        raise HTTPException(400, "Default 組織不可刪除")
    if org.id == user.organization_id:
        raise HTTPException(400, "不能刪除自己所屬的組織")
    await db.delete(org)
    await db.flush()


# ─── OrgInvite 邀請碼 ─────────────────────────────────────────────────

def _enrich_invite(inv: OrgInvite) -> dict:
    now = datetime.utcnow()
    is_used = inv.used_at is not None
    is_expired = inv.expires_at is not None and inv.expires_at < now
    return {
        "id": inv.id,
        "token": inv.token,
        "organization_id": inv.organization_id,
        "email": inv.email,
        "role_id": inv.role_id,
        "group_id": inv.group_id,
        "note": inv.note,
        "expires_at": inv.expires_at,
        "used_by": inv.used_by,
        "used_at": inv.used_at,
        "created_by": inv.created_by,
        "created_at": inv.created_at,
        "is_used": is_used,
        "is_expired": is_expired,
    }


@router.get("/invites", response_model=list[InviteResponse], tags=["X · 組織"])
async def list_invites(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """列邀請碼:superuser 看全部,其他人看自己 org 的。"""
    stmt = select(OrgInvite).order_by(OrgInvite.created_at.desc())
    if not user.is_superuser:
        stmt = stmt.where(OrgInvite.organization_id == user.organization_id)
    rows = (await db.execute(stmt)).scalars().all()
    return [_enrich_invite(r) for r in rows]


@router.post(
    "/invites", response_model=InviteResponse, status_code=201, tags=["X · 組織"]
)
async def create_invite(
    payload: InviteCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """建立邀請碼。一般 user 只能建自己 org 的;superuser 可指定任何 org。"""
    target_org_id = payload.organization_id or user.organization_id
    if not target_org_id:
        raise HTTPException(400, "organization_id 必填(或先把使用者掛到某個 org)")
    if not user.is_superuser and target_org_id != user.organization_id:
        raise HTTPException(403, "只能為自己所屬的 organization 建立邀請")
    org = await db.get(Organization, target_org_id)
    if not org:
        raise HTTPException(404, "Organization not found")

    days = max(1, min(365, int(payload.expires_in_days or 7)))
    token = secrets.token_urlsafe(24)  # 32-char base64-ish,URL safe
    inv = OrgInvite(
        token=token,
        organization_id=target_org_id,
        email=(payload.email or "").strip().lower() or None,
        role_id=payload.role_id,
        group_id=payload.group_id,
        note=payload.note,
        expires_at=datetime.utcnow() + timedelta(days=days),
        created_by=user.username,
    )
    db.add(inv)
    await db.flush()
    await db.refresh(inv)
    return _enrich_invite(inv)


@router.delete("/invites/{invite_id}", status_code=204, tags=["X · 組織"])
async def revoke_invite(
    invite_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """撤銷(刪除)邀請。已被使用的也可以刪(歷史紀錄走 audit log)。"""
    inv = await db.get(OrgInvite, invite_id)
    if not inv:
        raise HTTPException(404, "Invite not found")
    if not user.is_superuser and inv.organization_id != user.organization_id:
        raise HTTPException(404, "Invite not found")
    await db.delete(inv)
    await db.flush()
