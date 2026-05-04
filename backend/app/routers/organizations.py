"""Organization REST endpoints — superuser-only(部分端點開放給組織內 admin)。"""

import re
import secrets
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy import asc, desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.database import get_db
from app.models.org_invite import OrgInvite
from app.models.org_membership import OrgMembership
from app.models.organization import Organization
from app.models.role import Role
from app.models.user import User
from app.rate_limit import limiter

router = APIRouter()


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _domain_of(email: str) -> Optional[str]:
    email = (email or "").strip().lower()
    if "@" not in email:
        return None
    return email.rsplit("@", 1)[1].strip() or None


async def _org_for_domain(db: AsyncSession, domain: str) -> Optional[Organization]:
    """Return the Organization whose email_domains list contains `domain`, or None."""
    if not domain:
        return None
    rows = (await db.execute(select(Organization))).scalars().all()
    for o in rows:
        if not o.email_domains:
            continue
        domains = {d.strip().lower() for d in o.email_domains.split(",") if d.strip()}
        if domain in domains:
            return o
    return None


def _mask_email(email: str) -> str:
    """me@example.com -> m**@example.com (don't leak full address back)."""
    if "@" not in email:
        return "***"
    local, _, dom = email.partition("@")
    if len(local) <= 1:
        return f"*@{dom}"
    return f"{local[0]}{'*' * (len(local) - 1)}@{dom}"


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
    # Phase 4 follow-up — when True, the server enqueues an invite email
    # to `email` after minting (uses the same template as /request-access).
    # Requires `email` to be set; ignored otherwise.
    send_email: bool = False


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


async def _ensure_no_cross_org_dupe(
    db: AsyncSession, *, normalized: Optional[str], own_org_id: Optional[str],
) -> None:
    """Reject the write if any normalized domain is already claimed by a
    *different* organization. Same-org rewrites pass through.

    Phase 4D: turns the migration's NOTICE into an actual 409 so two orgs
    can never silently both own ``acme.com``."""
    if not normalized:
        return
    requested = {d.strip().lower() for d in normalized.split(",") if d.strip()}
    if not requested:
        return
    rows = (await db.execute(select(Organization))).scalars().all()
    conflicts: list[tuple[str, str]] = []  # (domain, conflicting_org_slug)
    for o in rows:
        if o.id == own_org_id or not o.email_domains:
            continue
        existing = {d.strip().lower() for d in o.email_domains.split(",") if d.strip()}
        for d in requested & existing:
            conflicts.append((d, o.slug))
    if conflicts:
        # Sort for deterministic error output, dedupe domain spam.
        seen: set[str] = set()
        msg_parts: list[str] = []
        for d, slug in conflicts:
            if d in seen:
                continue
            seen.add(d)
            msg_parts.append(f"{d} ↔ {slug}")
        raise HTTPException(
            status_code=409,
            detail={
                "error": "domain_conflict",
                "message": "下列 domain 已被其他組織登記:" + ", ".join(msg_parts),
                "conflicts": [{"domain": d, "owner_org_slug": s} for d, s in conflicts],
            },
        )


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
    normalized = _normalize_domains(payload.email_domains)
    await _ensure_no_cross_org_dupe(db, normalized=normalized, own_org_id=None)
    org = Organization(
        slug=payload.slug,
        name=payload.name,
        description=payload.description,
        plan=payload.plan or "free",
        email_domains=normalized,
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
    # Validate any incoming email_domains BEFORE applying any field, so we
    # don't half-update on a conflict.
    if "email_domains" in data:
        normalized = _normalize_domains(data["email_domains"])
        await _ensure_no_cross_org_dupe(db, normalized=normalized, own_org_id=org.id)
        org.email_domains = normalized
    for k, v in data.items():
        if k == "email_domains":
            continue  # handled above
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
    expires_at = datetime.utcnow() + timedelta(days=days)
    target_email = (payload.email or "").strip().lower() or None
    inv = OrgInvite(
        token=token,
        organization_id=target_org_id,
        email=target_email,
        role_id=payload.role_id,
        group_id=payload.group_id,
        note=payload.note,
        expires_at=expires_at,
        created_by=user.username,
    )
    db.add(inv)
    await db.flush()
    await db.refresh(inv)

    # Optional: also email the invite token to the recipient. Mirrors the
    # /request-access flow — same template, same Celery task. Failures
    # logged but never bubble; the invite row was already saved successfully.
    if payload.send_email and target_email:
        await _send_invite_email(inv, org, db)
    return _enrich_invite(inv)


async def _send_invite_email(inv: OrgInvite, org: Organization, db: AsyncSession) -> bool:
    """Enqueue the invite email via Celery. Updates inv.email_sent_at on success.
    Returns True if enqueued, False on failure (logged). Used by both create &
    resend so the two share one path."""
    target_email = inv.email or inv.email_sent_to
    if not target_email:
        return False
    try:
        from app.services.email_service import render_invite_email
        from tasks.email_tasks import send_email_task
        register_url = f"/register?token={inv.token}&email={target_email}"
        html_body, text_body = render_invite_email(
            org_name=org.name,
            register_url=register_url,
            token=inv.token,
            expires_at=inv.expires_at,
        )
        send_email_task.delay(
            to=target_email,
            subject=f"您獲邀加入 {org.name} (AutoTest)",
            html_body=html_body,
            text_body=text_body,
            organization_id=org.id,
        )
        inv.email_sent_at = datetime.utcnow()
        inv.email_sent_to = target_email
        await db.flush()
        return True
    except Exception:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).exception(
            "send_invite_email: enqueue failed for invite=%s email=%s", inv.id, target_email,
        )
        return False


# ─── C4: invite lifecycle — resend / extend / bulk ──────────────
@router.post("/invites/{invite_id}/resend", tags=["X · 組織"])
async def resend_invite(
    invite_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """重寄邀請信給原 email。若已被使用 → 409。已過期 → 403(請先 extend)。"""
    inv = await db.get(OrgInvite, invite_id)
    if not inv:
        raise HTTPException(404, "Invite not found")
    if not user.is_superuser and inv.organization_id != user.organization_id:
        raise HTTPException(404, "Invite not found")
    if inv.used_at is not None:
        raise HTTPException(409, "此邀請已被使用,無需重寄")
    if inv.expires_at and inv.expires_at < datetime.utcnow():
        raise HTTPException(403, "邀請已過期,請先延長有效期再重寄")
    if not (inv.email or inv.email_sent_to):
        raise HTTPException(400, "此邀請沒有 email,無法寄送(只能給對方手動傳 token)")
    org = await db.get(Organization, inv.organization_id)
    if not org:
        raise HTTPException(404, "Organization not found")
    ok = await _send_invite_email(inv, org, db)
    if not ok:
        raise HTTPException(500, "寄送失敗(已記錄到 server log)")
    return _enrich_invite(inv)


@router.patch("/invites/{invite_id}/extend", tags=["X · 組織"])
async def extend_invite(
    invite_id: str,
    days: int = 7,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """延長邀請有效期。`days` 是「從現在起再加 N 天」(不是從原 expires_at)。"""
    inv = await db.get(OrgInvite, invite_id)
    if not inv:
        raise HTTPException(404, "Invite not found")
    if not user.is_superuser and inv.organization_id != user.organization_id:
        raise HTTPException(404, "Invite not found")
    if inv.used_at is not None:
        raise HTTPException(409, "此邀請已被使用,無法延長")
    days_int = max(1, min(365, int(days or 7)))
    inv.expires_at = datetime.utcnow() + timedelta(days=days_int)
    await db.flush()
    return _enrich_invite(inv)


@router.post("/invites/bulk", tags=["X · 組織"])
async def bulk_create_invites(
    payload: dict,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """一次建立多筆邀請(給 admin 從 csv 貼上 N 個 email 用)。
    body:`{"emails": [...], "role_id": null, "group_id": null,
            "expires_in_days": 7, "send_email": true, "organization_id": null}`
    回傳:`{created: [InviteResponse...], skipped: [{email, reason}]}`,limit 200/call。"""
    emails_in = (payload or {}).get("emails") or []
    if not isinstance(emails_in, list) or not emails_in:
        raise HTTPException(400, "缺少 emails(非空陣列)")
    if len(emails_in) > 200:
        raise HTTPException(400, "一次最多 200 筆")
    target_org_id = (payload or {}).get("organization_id") or user.organization_id
    if not target_org_id:
        raise HTTPException(400, "organization_id 必填")
    if not user.is_superuser and target_org_id != user.organization_id:
        raise HTTPException(403, "只能為自己所屬的 organization 建立邀請")
    org = await db.get(Organization, target_org_id)
    if not org:
        raise HTTPException(404, "Organization not found")
    role_id = (payload or {}).get("role_id") or None
    group_id = (payload or {}).get("group_id") or None
    days = max(1, min(365, int((payload or {}).get("expires_in_days") or 7)))
    send_email = bool((payload or {}).get("send_email", True))
    seen: set[str] = set()
    created: list[dict] = []
    skipped: list[dict] = []
    for raw in emails_in:
        e = (raw or "").strip().lower()
        if not e:
            skipped.append({"email": raw, "reason": "空字串"}); continue
        if not _EMAIL_RE.match(e):
            skipped.append({"email": e, "reason": "格式錯誤"}); continue
        if e in seen:
            skipped.append({"email": e, "reason": "本次重複"}); continue
        seen.add(e)
        token = secrets.token_urlsafe(24)
        inv = OrgInvite(
            token=token,
            organization_id=target_org_id,
            email=e,
            role_id=role_id,
            group_id=group_id,
            expires_at=datetime.utcnow() + timedelta(days=days),
            created_by=user.username,
        )
        db.add(inv)
        await db.flush()
        await db.refresh(inv)
        if send_email:
            await _send_invite_email(inv, org, db)
        created.append(_enrich_invite(inv))
    return {"created": created, "skipped": skipped}


# ── Self-service flow (Phase 4) — anonymous endpoints ────────────────────

class ByEmailDomainResponse(BaseModel):
    organization_id: str
    organization_slug: str
    organization_name: str


@router.get(
    "/organizations/by-email-domain",
    response_model=ByEmailDomainResponse,
    tags=["X · 組織"],
)
@limiter.limit("30/minute")
async def by_email_domain(
    request: Request,
    email: str,
    db: AsyncSession = Depends(get_db),
):
    """Anonymous lookup: which org owns this email's @domain?

    Used by the front-end self-service form to show a live hint
    (\"You will be invited into {org}\") before submission. 404 if the
    domain is not claimed."""
    domain = _domain_of(email)
    if not domain:
        raise HTTPException(400, "invalid email")
    org = await _org_for_domain(db, domain)
    if not org:
        raise HTTPException(404, "domain not registered")
    return ByEmailDomainResponse(
        organization_id=org.id,
        organization_slug=org.slug,
        organization_name=org.name,
    )


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


# ─────────────────── Org Members CRUD(多組織用)───────────────────────
# 一個 user 可在多個 org 各佔一筆 OrgMembership;這幾個 endpoint 是給 org admin
# 在「組織成員」UI 用,可以新增現有 user 進來、改角色、移除等。
# 嚴格走 org_id 路徑參數 + 權限檢查(只有 superuser 或同 org admin 能動)。

def _can_manage_org(user: User, org_id: str) -> bool:
    return bool(user.is_superuser) or user.organization_id == org_id


# ─── C2: email-domain preview validator ──────────────────────────
@router.get("/orgs/{org_id}/users-matching-domain", tags=["X · 組織"])
async def users_matching_domain(
    org_id: str,
    domain: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """C2 — 設定 email_domains 前的預覽器:列出所有 email 結尾符合 `domain` 的
    註冊 user,並標示哪些已經是此 org 的成員 / 哪些是「會被新加進來」。

    回傳:`{domain, matched_users, would_join_count, already_in_count}`
    - `matched_users[].current_org_id` = 其 default OrgMembership 所在 org;
      若已在此 org → `already_in_count` 計數,否則 `would_join_count` 計數。
    """
    if not _can_manage_org(user, org_id):
        raise HTTPException(404, "Organization not found")
    d = (domain or "").strip().lower().lstrip("@")
    if not d:
        raise HTTPException(400, "缺少 domain")
    # email like '%@d'(小寫比對,容忍欄位是空字串的 user)
    rows = (
        await db.execute(
            select(User).where(func.lower(User.email).like(f"%@{d}"))
        )
    ).scalars().all()
    if not rows:
        return {"domain": d, "matched_users": [], "would_join_count": 0, "already_in_count": 0}
    # 一次撈 OrgMembership 給這批 user(避免 N+1)
    usernames = [u.username for u in rows]
    mem_rows = (
        await db.execute(
            select(OrgMembership)
            .where(OrgMembership.username.in_(usernames))
        )
    ).scalars().all()
    in_this_org = {m.username for m in mem_rows if m.organization_id == org_id}
    default_org = {m.username: m.organization_id for m in mem_rows if m.is_default}
    out = []
    would_join = 0
    already_in = 0
    for u in rows:
        is_in = u.username in in_this_org
        if is_in:
            already_in += 1
        else:
            would_join += 1
        out.append({
            "username": u.username,
            "display_name": u.display_name,
            "email": u.email,
            "current_org_id": default_org.get(u.username),
            "is_in_this_org": is_in,
        })
    out.sort(key=lambda x: (x["is_in_this_org"], x["username"]))
    return {
        "domain": d,
        "matched_users": out,
        "would_join_count": would_join,
        "already_in_count": already_in,
    }


_ORG_MEMBER_SORT_COLS = {
    "username": User.username,
    "email": User.email,
    "display_name": User.display_name,
    "role_name": Role.name,
    "status": OrgMembership.status,
    "joined_at": OrgMembership.joined_at,
}


@router.get("/orgs/{org_id}/members", tags=["X · 組織"])
async def list_org_members(
    org_id: str,
    response: Response,
    search: Optional[str] = None,
    sort_by: Optional[str] = None,
    sort_dir: str = "asc",
    limit: Optional[int] = None,
    offset: int = 0,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """列出組織 OrgMembership。可選 `?search=&sort_by=&sort_dir=&limit=&offset=`。
    當帶 `limit` 時 response header 會有 `X-Total-Count`(unfiltered-by-page count)。
    無 limit → 回全部(backward compat)。"""
    if not _can_manage_org(user, org_id):
        raise HTTPException(404, "Organization not found")

    base_join = (
        select(OrgMembership, User, Role)
        .join(User, User.username == OrgMembership.username)
        .outerjoin(Role, Role.id == OrgMembership.role_id)
        .where(OrgMembership.organization_id == org_id)
    )
    count_join = (
        select(func.count())
        .select_from(OrgMembership)
        .join(User, User.username == OrgMembership.username)
        .outerjoin(Role, Role.id == OrgMembership.role_id)
        .where(OrgMembership.organization_id == org_id)
    )

    if search:
        q = f"%{search.strip().lower()}%"
        cond = or_(
            func.lower(User.username).like(q),
            func.lower(func.coalesce(User.email, "")).like(q),
            func.lower(func.coalesce(User.display_name, "")).like(q),
        )
        base_join = base_join.where(cond)
        count_join = count_join.where(cond)

    sort_col = _ORG_MEMBER_SORT_COLS.get((sort_by or "").strip()) or User.username
    direction = desc if (sort_dir or "asc").lower() == "desc" else asc
    base_join = base_join.order_by(direction(sort_col))

    if limit is not None:
        total = (await db.execute(count_join)).scalar_one() or 0
        response.headers["X-Total-Count"] = str(total)
        try:
            limit_int = max(1, min(int(limit), 500))
        except (TypeError, ValueError):
            limit_int = 50
        try:
            offset_int = max(0, int(offset))
        except (TypeError, ValueError):
            offset_int = 0
        base_join = base_join.limit(limit_int).offset(offset_int)

    rows = (await db.execute(base_join)).all()
    return [
        {
            "id": mem.id,
            "username": u.username,
            "display_name": u.display_name,
            "email": u.email,
            "role_id": role.id if role else None,
            "role_name": role.name if role else None,
            "is_default": bool(mem.is_default),
            "status": mem.status,
            "joined_at": mem.joined_at.isoformat() if mem.joined_at else None,
        }
        for mem, u, role in rows
    ]


@router.post("/orgs/{org_id}/members", status_code=201, tags=["X · 組織"])
async def add_org_member(
    org_id: str,
    payload: dict,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """把現有使用者加進組織。body `{"username": "...", "role_id": "..." | null}`。
    若該 user 已是成員 → 409。
    寄邀請給沒帳號的人請改用 `POST /api/invites`。"""
    if not _can_manage_org(user, org_id):
        raise HTTPException(404, "Organization not found")
    target_username = (payload or {}).get("username", "").strip()
    role_id = (payload or {}).get("role_id") or None
    if not target_username:
        raise HTTPException(400, "缺少 username")
    target = await db.get(User, target_username)
    if not target:
        raise HTTPException(404, "找不到該使用者")
    org = await db.get(Organization, org_id)
    if not org:
        raise HTTPException(404, "找不到該組織")
    if role_id:
        role = await db.get(Role, role_id)
        if not role or (role.organization_id and role.organization_id != org_id):
            raise HTTPException(400, "無效的 role_id(不存在或不屬於此組織)")
    existing = (
        await db.execute(
            select(OrgMembership)
            .where(OrgMembership.username == target_username)
            .where(OrgMembership.organization_id == org_id)
        )
    ).scalar_one_or_none()
    if existing:
        raise HTTPException(409, "該使用者已是組織成員")
    mem = OrgMembership(
        username=target_username,
        organization_id=org_id,
        role_id=role_id,
        is_default=False,    # 由使用者自行 switch 設 default
        status="active",
        invited_by=user.username,
    )
    db.add(mem)
    await db.flush()
    return {"id": mem.id, "username": mem.username, "organization_id": mem.organization_id}


# ─── Tier B5:bulk 改角色 / 狀態(讓 admin 一次改 N 人) ─────────────
@router.patch("/orgs/{org_id}/members/bulk", tags=["X · 組織"])
async def bulk_update_org_members(
    org_id: str,
    payload: dict,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """一次更新多筆 OrgMembership 的 role_id / status。
    body: `{"usernames": ["a", "b", ...], "role_id": "..." | null, "status": "active"}`
    transaction 內單批 update,部分失敗(找不到使用者)會跳過該筆但繼續其他人。
    回傳 `{updated: N, skipped: [{username, reason}, ...]}`。
    """
    if not _can_manage_org(user, org_id):
        raise HTTPException(404, "Organization not found")
    body = payload or {}
    usernames = body.get("usernames") or []
    if not isinstance(usernames, list) or not usernames:
        raise HTTPException(400, "缺少 usernames(非空陣列)")
    if len(usernames) > 200:
        raise HTTPException(400, "單次最多 200 筆")

    # 驗證 role_id(若有)— 在 loop 外驗一次,不要每筆都打 DB
    has_role = "role_id" in body
    role_id = body.get("role_id") or None if has_role else None
    if has_role and role_id:
        role = await db.get(Role, role_id)
        if not role or (role.organization_id and role.organization_id != org_id):
            raise HTTPException(400, "無效的 role_id")

    has_status = "status" in body
    new_status = (body.get("status") or "").strip() if has_status else None
    if has_status and new_status not in ("active", "invited", "disabled"):
        raise HTTPException(400, "status 必須是 active / invited / disabled")

    if not has_role and not has_status:
        raise HTTPException(400, "至少要指定 role_id 或 status 其中一個")

    updated = 0
    skipped: list[dict] = []
    for u in usernames:
        u = (u or "").strip()
        if not u:
            continue
        mem = (await db.execute(
            select(OrgMembership)
            .where(OrgMembership.username == u)
            .where(OrgMembership.organization_id == org_id)
        )).scalar_one_or_none()
        if not mem:
            skipped.append({"username": u, "reason": "not a member"})
            continue
        if has_role:
            mem.role_id = role_id
        if has_status:
            mem.status = new_status
        updated += 1
    await db.flush()
    return {"updated": updated, "skipped": skipped}


@router.patch("/orgs/{org_id}/members/{username}", tags=["X · 組織"])
async def update_org_member(
    org_id: str,
    username: str,
    payload: dict,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """改 OrgMembership 的 role_id 或 status。body 可含 `role_id` / `status`。"""
    if not _can_manage_org(user, org_id):
        raise HTTPException(404, "Organization not found")
    mem = (
        await db.execute(
            select(OrgMembership)
            .where(OrgMembership.username == username)
            .where(OrgMembership.organization_id == org_id)
        )
    ).scalar_one_or_none()
    if not mem:
        raise HTTPException(404, "找不到此成員")
    if "role_id" in (payload or {}):
        role_id = payload["role_id"] or None
        if role_id:
            role = await db.get(Role, role_id)
            if not role or (role.organization_id and role.organization_id != org_id):
                raise HTTPException(400, "無效的 role_id")
        mem.role_id = role_id
    if "status" in (payload or {}):
        new_status = (payload["status"] or "").strip()
        if new_status not in ("active", "invited", "disabled"):
            raise HTTPException(400, "status 必須是 active / invited / disabled")
        mem.status = new_status
    await db.flush()
    return {"ok": True, "id": mem.id, "role_id": mem.role_id, "status": mem.status}


@router.delete("/orgs/{org_id}/members/{username}", status_code=204, tags=["X · 組織"])
async def delete_org_member(
    org_id: str,
    username: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """從組織移除成員。OrgMembership 刪除後,該 user 在此 org 的所有 ProjectMember
    也會 cascade-delete(走 FK ondelete=CASCADE)— 但要寫 SQL 強制清,
    因為 ProjectMember 是綁 username + project,不會自動跟 OrgMembership 連動。
    """
    if not _can_manage_org(user, org_id):
        raise HTTPException(404, "Organization not found")
    mem = (
        await db.execute(
            select(OrgMembership)
            .where(OrgMembership.username == username)
            .where(OrgMembership.organization_id == org_id)
        )
    ).scalar_one_or_none()
    if not mem:
        raise HTTPException(404, "找不到此成員")
    # 不允許自我移除(避免自鎖在門外)
    if username == user.username and not user.is_superuser:
        raise HTTPException(400, "不可移除自己;請改設 status=disabled 或請其他 admin 操作")
    await db.delete(mem)
    # 同時清掉 user 在此 org 下所有 project 的 ProjectMember
    from app.models.project import Project
    from app.models.project_member import ProjectMember
    project_ids = (
        await db.execute(select(Project.id).where(Project.organization_id == org_id))
    ).scalars().all()
    if project_ids:
        await db.execute(
            ProjectMember.__table__.delete()
            .where(ProjectMember.username == username)
            .where(ProjectMember.project_id.in_(project_ids))
        )
    await db.flush()
