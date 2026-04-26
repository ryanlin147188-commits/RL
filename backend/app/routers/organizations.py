"""Organization REST endpoints — superuser-only。"""
from __future__ import annotations

import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.database import get_db
from app.models.organization import Organization
from app.models.user import User

router = APIRouter()


class OrgCreate(BaseModel):
    slug: str
    name: str
    description: Optional[str] = None
    plan: Optional[str] = "free"


class OrgUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    plan: Optional[str] = None


class OrgResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    slug: str
    name: str
    description: Optional[str] = None
    plan: Optional[str] = None


def _require_superuser(user: User) -> None:
    if not user.is_superuser:
        raise HTTPException(403, "需要 superuser 權限")


SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,58}$")


@router.get("/organizations", response_model=list[OrgResponse], tags=["X · 組織"])
async def list_orgs(
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    _require_superuser(user)
    rows = (await db.execute(select(Organization).order_by(Organization.slug))).scalars().all()
    return list(rows)


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
        if v is not None:
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
