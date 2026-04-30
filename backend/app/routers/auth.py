"""Auth REST endpoints — 登入 / 登出 / 取得當前使用者 / 變更密碼 / 管理使用者。

注意：此檔不寫 `from __future__ import annotations`；slowapi 的 @limiter.limit
裝飾器會讀取 function signature 做型別內省，搭配延後求值的 forward-ref
（如 `payload: LoginRequest`）會在 FastAPI 註冊路由時拋
`PydanticUndefinedAnnotation`。
"""
import io
import os
import re
import secrets
import uuid
from datetime import datetime, timedelta
from typing import Optional

import jwt as pyjwt
from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.storage_service import save_bytes

from app.auth.dependencies import get_current_user
from app.auth.revocation import revoke as revoke_jti
from app.auth.security import (
    ACCESS_TOKEN_TTL_MINUTES,
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)
from app.rate_limit import limiter
from app.database import get_db
from app.models.group import GroupMembership
from app.models.org_invite import OrgInvite
from app.models.organization import Organization
from app.models.role import Role
from app.models.user import User
from app.schemas.auth import (
    BootstrapInviteRequest,
    BootstrapInviteResponse,
    ChangePasswordRequest,
    LoginRequest,
    RedeemInviteRequest,
    RedeemInviteResponse,
    RefreshRequest,
    RegisterRequest,
    RequestAccessRequest,
    RequestAccessResponse,
    TokenResponse,
    UserCreateRequest,
    UserResponse,
    UserUpdateMeRequest,
)

router = APIRouter()


@router.post("/auth/login", response_model=TokenResponse, tags=["U · 認證"])
@limiter.limit("10/minute")          # 暴力破解防護：同一 IP 每分鐘最多 10 次登入嘗試
async def login(request: Request, payload: LoginRequest, db: AsyncSession = Depends(get_db)):
    user = (
        await db.execute(select(User).where(User.username == payload.username))
    ).scalar_one_or_none()
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="帳號或密碼錯誤")
    if not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="帳號或密碼錯誤")
    user.last_login_at = datetime.utcnow()
    await db.flush()
    # 把 organization_id 塞進 JWT，避免每個 request 都要 lookup user
    extra = {"org_id": user.organization_id, "is_superuser": user.is_superuser}
    return TokenResponse(
        access_token=create_access_token(user.username, extra=extra),
        refresh_token=create_refresh_token(user.username),
        expires_in=ACCESS_TOKEN_TTL_MINUTES * 60,
    )


USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,32}$")


@router.post("/auth/register", response_model=TokenResponse, status_code=201, tags=["U · 認證"])
@limiter.limit("5/minute")  # 防爆量註冊
async def register(
    request: Request,
    payload: RegisterRequest,
    db: AsyncSession = Depends(get_db),
):
    """自助註冊。歸屬邏輯:
        1. invite_token → 套用 invite 設定(org / role / group)
        2. email 後綴 match 某 org 的 email_domains → 自動加入該 org
        3. 都不 match → 拒絕(避免亂註冊污染 default org)
    註冊成功直接簽 access token,前端可立即登入。"""
    # ── 基本驗證 ──
    uname = (payload.username or "").strip()
    pwd = payload.password or ""
    email = (payload.email or "").strip().lower() or None
    if not USERNAME_RE.match(uname):
        raise HTTPException(400, "使用者名稱格式錯誤(3-32 字元,英數底線)")
    if len(pwd) < 6:
        raise HTTPException(400, "密碼至少 6 字元")
    existing = (
        await db.execute(select(User).where(User.username == uname))
    ).scalar_one_or_none()
    if existing:
        raise HTTPException(409, "帳號已存在")

    target_org: Optional[Organization] = None
    target_role_id: Optional[str] = None
    target_group_id: Optional[str] = None
    invite: Optional[OrgInvite] = None

    # 1) 邀請碼路徑
    if payload.invite_token:
        invite = (
            await db.execute(
                select(OrgInvite).where(OrgInvite.token == payload.invite_token.strip())
            )
        ).scalar_one_or_none()
        if not invite:
            raise HTTPException(400, "邀請碼無效")
        if invite.used_at is not None:
            raise HTTPException(400, "邀請碼已被使用")
        if invite.expires_at and invite.expires_at < datetime.utcnow():
            raise HTTPException(400, "邀請碼已過期")
        if invite.email and (not email or invite.email.lower() != email):
            raise HTTPException(400, "此邀請碼限定特定 email,請用該 email 註冊")
        target_org = await db.get(Organization, invite.organization_id)
        if not target_org:
            raise HTTPException(400, "邀請對應的組織不存在")
        target_role_id = invite.role_id
        target_group_id = invite.group_id

    # 2) Email domain 路徑
    if not target_org and email and "@" in email:
        domain = email.rsplit("@", 1)[1].strip().lower()
        if domain:
            orgs = (await db.execute(select(Organization))).scalars().all()
            for org in orgs:
                if not org.email_domains:
                    continue
                domains = {d.strip().lower() for d in org.email_domains.split(",") if d.strip()}
                if domain in domains:
                    target_org = org
                    break

    if not target_org:
        raise HTTPException(
            400,
            "找不到對應的組織。請聯絡管理員索取邀請碼,或使用組織註冊過的 Email 域名。",
        )

    # 預設角色(invite 沒指定就掛系統 Viewer,讓使用者進來只能讀;管理員之後再升)
    if not target_role_id:
        viewer = (
            await db.execute(
                select(Role).where(Role.name == "Viewer", Role.is_system.is_(True))
            )
        ).scalar_one_or_none()
        target_role_id = viewer.id if viewer else None

    new_user = User(
        username=uname,
        display_name=payload.display_name or uname,
        email=email,
        password_hash=hash_password(pwd),
        role_id=target_role_id,
        organization_id=target_org.id,
        is_superuser=False,
        is_active=True,
    )
    db.add(new_user)
    await db.flush()

    # 起始群組
    if target_group_id:
        # 確認 group 存在 + 同 org
        from app.models.group import Group
        g = await db.get(Group, target_group_id)
        if g and g.organization_id == target_org.id:
            db.add(GroupMembership(
                group_id=g.id, username=new_user.username, role_in_group="member"
            ))

    # 標記邀請已使用
    if invite is not None:
        invite.used_by = new_user.username
        invite.used_at = datetime.utcnow()

    await db.flush()

    # 自動登入:簽 access token + refresh token
    extra = {"org_id": new_user.organization_id, "is_superuser": new_user.is_superuser}
    return TokenResponse(
        access_token=create_access_token(new_user.username, extra=extra),
        refresh_token=create_refresh_token(new_user.username),
        expires_in=ACCESS_TOKEN_TTL_MINUTES * 60,
    )


@router.post(
    "/auth/redeem-invite",
    response_model=RedeemInviteResponse,
    tags=["U · 認證"],
)
async def redeem_invite(
    payload: RedeemInviteRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Already-logged-in user pastes an invite code to switch into the
    invite's organization (and optionally pick up the invite's role / group).

    Why a separate endpoint and not just register-with-token?
        Self-service registration was simplified to email-domain only;
        invite codes are now redeemed *after* login from Settings → 兌換邀請碼.
        This means a user who got auto-assigned to the wrong org (or whose
        company hasn't registered an email domain) can still join the right
        org by pasting the code their admin sent them.

    Validation mirrors the original register flow:
      * token must exist, not be used, not be expired
      * if invite has email-lock, it must match the caller's email
      * caller must have an email on file (otherwise email-locked invites
        can't be safely matched)

    On success:
      * caller's user row is updated: organization_id, optionally role_id
      * if the invite has a group_id, caller is added to GroupMembership
      * invite is marked used (single-use)
      * a fresh access/refresh token pair is returned so the new org_id
        flows into the JWT claim without the client having to re-login.
    """
    token = (payload.invite_token or "").strip()
    if not token:
        raise HTTPException(400, "請輸入邀請碼")

    invite = (
        await db.execute(select(OrgInvite).where(OrgInvite.token == token))
    ).scalar_one_or_none()
    if not invite:
        raise HTTPException(400, "邀請碼無效")
    if invite.used_at is not None:
        raise HTTPException(400, "邀請碼已被使用")
    if invite.expires_at and invite.expires_at < datetime.utcnow():
        raise HTTPException(400, "邀請碼已過期")

    if invite.email:
        caller_email = (user.email or "").strip().lower()
        if not caller_email:
            raise HTTPException(
                400,
                "此邀請碼限定特定 Email,請先到「設定 → 個人資料」設定您的 Email 後再兌換",
            )
        if invite.email.lower() != caller_email:
            raise HTTPException(400, "此邀請碼限定特定 Email,與您帳號的 Email 不符")

    target_org = await db.get(Organization, invite.organization_id)
    if not target_org:
        raise HTTPException(400, "邀請對應的組織不存在")

    # Apply: org switch + optional role + optional group
    user.organization_id = target_org.id
    role_assigned: Optional[str] = None
    if invite.role_id:
        role = await db.get(Role, invite.role_id)
        if role:
            user.role_id = role.id
            role_assigned = role.name

    group_assigned: Optional[str] = None
    if invite.group_id:
        from app.models.group import Group
        g = await db.get(Group, invite.group_id)
        if g and g.organization_id == target_org.id:
            # Idempotent: don't add a duplicate membership row.
            existing = (
                await db.execute(
                    select(GroupMembership).where(
                        GroupMembership.group_id == g.id,
                        GroupMembership.username == user.username,
                    )
                )
            ).scalar_one_or_none()
            if existing is None:
                db.add(GroupMembership(
                    group_id=g.id, username=user.username, role_in_group="member",
                ))
            group_assigned = g.name

    invite.used_by = user.username
    invite.used_at = datetime.utcnow()
    await db.flush()

    extra = {"org_id": user.organization_id, "is_superuser": user.is_superuser}
    return RedeemInviteResponse(
        organization_slug=target_org.slug,
        organization_name=target_org.name,
        role_assigned=role_assigned,
        group_assigned=group_assigned,
        access_token=create_access_token(user.username, extra=extra),
        refresh_token=create_refresh_token(user.username),
        expires_in=ACCESS_TOKEN_TTL_MINUTES * 60,
    )


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _domain_of(email: str) -> Optional[str]:
    email = (email or "").strip().lower()
    if "@" not in email:
        return None
    return email.rsplit("@", 1)[1].strip() or None


def _mask_email(email: str) -> str:
    if "@" not in email:
        return "***"
    local, _, dom = email.partition("@")
    if len(local) <= 1:
        return f"*@{dom}"
    return f"{local[0]}{'*' * (len(local) - 1)}@{dom}"


@router.post(
    "/auth/request-access",
    response_model=RequestAccessResponse,
    status_code=202,
    tags=["U · 認證"],
)
@limiter.limit("5/hour")
async def request_access(
    request: Request,
    payload: RequestAccessRequest,
    db: AsyncSession = Depends(get_db),
):
    """Anonymous self-service invite.

    Flow:
      1. Caller posts ``email`` (and optional ``display_name``).
      2. Server resolves the email's @domain → Organization via
         ``email_domains``. No match → 400 ``unknown_domain``.
      3. Server mints a single-use OrgInvite (Viewer role, 24h TTL,
         email-bound) and enqueues an invite email containing the token.
      4. Server returns 202 with ``{sent, organization_slug, masked_email}``.
         The token itself is NEVER in the response — only in the email —
         so a leaked HTTP log doesn't leak invite redemption power.

    Rate limit: 5/hour per IP (slowapi). Single-email cooldown is enforced
    inline below so the same address can't be re-mailed in under 60s.
    """
    email = (payload.email or "").strip().lower()
    if not _EMAIL_RE.match(email):
        raise HTTPException(400, "請輸入有效的 email")
    domain = _domain_of(email)
    if not domain:
        raise HTTPException(400, "請輸入有效的 email")

    # 1) Find the org claiming this domain
    orgs = (await db.execute(select(Organization))).scalars().all()
    target_org: Optional[Organization] = None
    for o in orgs:
        if not o.email_domains:
            continue
        domains = {d.strip().lower() for d in o.email_domains.split(",") if d.strip()}
        if domain in domains:
            target_org = o
            break
    if not target_org:
        raise HTTPException(
            status_code=400,
            detail={"error": "unknown_domain", "message": "此 email 域名尚未登記任何組織,請聯絡管理員"},
        )

    # 2) Per-email cooldown: don't re-mail the same address within 60 seconds
    now = datetime.utcnow()
    recent = (
        await db.execute(
            select(OrgInvite)
            .where(OrgInvite.email == email)
            .where(OrgInvite.organization_id == target_org.id)
            .order_by(OrgInvite.created_at.desc())
        )
    ).scalars().first()
    if recent and recent.email_sent_at and (now - recent.email_sent_at).total_seconds() < 60:
        raise HTTPException(
            status_code=429,
            detail="邀請信已在 1 分鐘內寄出,請稍候再試",
        )

    # 3) Default to Viewer role (admin can promote later)
    viewer = (
        await db.execute(
            select(Role).where(Role.name == "Viewer", Role.is_system.is_(True))
        )
    ).scalar_one_or_none()

    # 4) Mint the invite
    expires_at = now + timedelta(hours=24)
    invite_token = "REQ-" + secrets.token_urlsafe(24)
    invite = OrgInvite(
        organization_id=target_org.id,
        token=invite_token,
        email=email,
        role_id=viewer.id if viewer else None,
        expires_at=expires_at,
        note="self-service request-access",
        email_sent_at=now,
        email_sent_to=email,
    )
    db.add(invite)
    await db.flush()

    # 5) Enqueue the invite email (non-blocking; failures logged but ignored)
    try:
        from app.services.email_service import render_invite_email
        from tasks.email_tasks import send_email_task

        # Best-effort register URL: the front-end consumes ?token=&email=
        # Anchor on the request host so dev/prod both work without config.
        register_url = (
            f"{request.url.scheme}://{request.url.netloc}/register"
            f"?token={invite_token}&email={email}"
        )
        html_body, text_body = render_invite_email(
            org_name=target_org.name,
            register_url=register_url,
            token=invite_token,
            expires_at=expires_at,
        )
        send_email_task.delay(
            to=email,
            subject=f"您獲邀加入 {target_org.name} (AutoTest)",
            html_body=html_body,
            text_body=text_body,
            organization_id=target_org.id,
        )
    except Exception:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).exception(
            "request-access: invite saved but email enqueue failed for %s", email,
        )

    return RequestAccessResponse(
        sent=True,
        organization_slug=target_org.slug,
        masked_email=_mask_email(email),
    )


@router.post(
    "/auth/bootstrap-invite",
    response_model=BootstrapInviteResponse,
    status_code=201,
    tags=["U · 認證"],
)
@limiter.limit("3/hour")
async def bootstrap_invite(
    request: Request,
    payload: BootstrapInviteRequest,
    db: AsyncSession = Depends(get_db),
):
    """Mint the first Admin invite token for a freshly-deployed organisation.

    The endpoint is **disabled by default**. Operator must:

    1. Set ``AUTOTEST_BOOTSTRAP_TOKEN`` to a strong random value
       (e.g. ``openssl rand -hex 32``) and restart the backend.
    2. Hit this endpoint with the matching token in the request body.

    Once any active admin (superuser OR Admin role) exists in the target
    organisation the endpoint returns 409 — preventing accidental
    re-bootstrap on a running deploy.

    Use the returned ``invite_token`` in ``POST /api/auth/register`` to
    create the first Admin user. After that, ``unset
    AUTOTEST_BOOTSTRAP_TOKEN`` and restart so the door closes behind you.
    """
    # ── Gate 1: operator-controlled secret ─────────────────────────────
    expected = (os.environ.get("AUTOTEST_BOOTSTRAP_TOKEN") or "").strip()
    if not expected:
        raise HTTPException(
            status_code=503,
            detail=(
                "bootstrap-invite is disabled. Operator must set "
                "AUTOTEST_BOOTSTRAP_TOKEN env var and restart the backend "
                "to enable. See docs/ops/bootstrap.md."
            ),
        )
    # constant-time compare to avoid timing-based token enumeration
    if not secrets.compare_digest(payload.bootstrap_token or "", expected):
        raise HTTPException(status_code=403, detail="bootstrap_token mismatch")

    # ── Resolve target org ─────────────────────────────────────────────
    org = (
        await db.execute(
            select(Organization).where(Organization.slug == payload.organization_slug)
        )
    ).scalar_one_or_none()
    if not org:
        raise HTTPException(
            status_code=404,
            detail=f"organization '{payload.organization_slug}' not found",
        )

    # ── Gate 2: no admin must exist yet ────────────────────────────────
    admin_role = (
        await db.execute(select(Role).where(Role.name == "Admin", Role.is_system.is_(True)))
    ).scalar_one_or_none()
    if not admin_role:
        # Should never happen because lifespan seeds Admin/QA/Viewer, but
        # fail loud rather than silently mint an invite to nowhere.
        raise HTTPException(
            status_code=500,
            detail="default Admin role not seeded; contact operator",
        )

    admin_count_q = (
        select(func.count(User.username))
        .outerjoin(Role, User.role_id == Role.id)
        .where(
            User.organization_id == org.id,
            User.is_active.is_(True),
            or_(User.is_superuser.is_(True), Role.name == "Admin"),
        )
    )
    admin_count = (await db.execute(admin_count_q)).scalar_one() or 0
    if admin_count > 0:
        raise HTTPException(
            status_code=409,
            detail=(
                f"organization '{org.slug}' already has {admin_count} active "
                "admin user(s); have one of them mint an invite via the "
                "regular admin UI instead."
            ),
        )

    # ── Mint the invite ────────────────────────────────────────────────
    ttl = max(1, min(payload.ttl_hours, 24 * 7))  # clamp 1h..7d
    expires_at = datetime.utcnow() + timedelta(hours=ttl)
    invite_token = "BOOT-" + secrets.token_urlsafe(24)

    invite = OrgInvite(
        organization_id=org.id,
        token=invite_token,
        email=(payload.email or "").strip().lower() or None,
        role_id=admin_role.id,
        expires_at=expires_at,
    )
    db.add(invite)
    await db.flush()

    return BootstrapInviteResponse(
        invite_token=invite_token,
        organization_id=org.id,
        organization_slug=org.slug,
        role="Admin",
        expires_at=expires_at,
    )


@router.post("/auth/refresh", response_model=TokenResponse, tags=["U · 認證"])
async def refresh_token(payload: RefreshRequest, db: AsyncSession = Depends(get_db)):
    try:
        decoded = decode_token(payload.refresh_token)
    except pyjwt.ExpiredSignatureError:
        raise HTTPException(401, "Refresh token 已過期，請重新登入")
    except pyjwt.PyJWTError:
        raise HTTPException(401, "Refresh token 無效")
    if decoded.get("typ") != "refresh":
        raise HTTPException(401, "需要 refresh token")
    username = decoded.get("sub")
    user = (
        await db.execute(select(User).where(User.username == username))
    ).scalar_one_or_none()
    if not user or not user.is_active:
        raise HTTPException(401, "使用者不存在或已停用")
    extra = {"org_id": user.organization_id, "is_superuser": user.is_superuser}
    return TokenResponse(
        access_token=create_access_token(user.username, extra=extra),
        refresh_token=create_refresh_token(user.username),
        expires_in=ACCESS_TOKEN_TTL_MINUTES * 60,
    )


@router.post("/auth/logout", status_code=204, tags=["U · 認證"])
async def logout(request: Request, user: User = Depends(get_current_user)):
    """Revoke the access token used for this request.

    The token is added to the Valkey blocklist with a TTL equal to its
    remaining lifetime, after which the entry expires automatically. The
    refresh token is NOT revoked here — to invalidate everything for a user
    rotate their password (drives a separate cascade) or have an admin
    deactivate the account.
    """
    payload = getattr(request.state, "user_payload", None) or {}
    await revoke_jti(payload.get("jti"), payload.get("exp"))
    return None


@router.get("/auth/me", response_model=UserResponse, tags=["U · 認證"])
async def me(user: User = Depends(get_current_user)):
    return user


@router.put("/auth/me", response_model=UserResponse, tags=["U · 認證"])
async def update_me(
    payload: UserUpdateMeRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """目前登入的使用者更新自己的個人資料。
    包含 display_name / email / role_id;不能改 username 或 password
    (改 password 走 /auth/change-password)。
    """
    if payload.display_name is not None:
        user.display_name = payload.display_name.strip()[:120] or None
    if payload.email is not None:
        user.email = payload.email.strip()[:255] or None
    if payload.role_id is not None:
        # 驗證角色存在(可空,代表無角色)
        if payload.role_id:
            role = (await db.execute(select(Role).where(Role.id == payload.role_id))).scalar_one_or_none()
            if not role:
                raise HTTPException(404, "找不到該角色")
            user.role_id = payload.role_id
        else:
            user.role_id = None
    await db.flush()
    await db.refresh(user)
    return user


@router.post("/auth/me/avatar", response_model=UserResponse, tags=["U · 認證"])
async def upload_avatar(
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """上傳大頭貼到 SeaweedFS pic bucket;路徑格式 avatars/<username>/<uuid>.<ext>。

    限制:
    - MIME type 必須是 image/*
    - 檔案 ≤ 5 MB
    - 不刪舊檔(歷史保留;之後想清乾淨可另開 cleanup task)
    """
    if not (file.content_type or "").startswith("image/"):
        raise HTTPException(400, "請上傳圖片檔(image/*)")
    raw = await file.read()
    if len(raw) > 5 * 1024 * 1024:
        raise HTTPException(400, "檔案過大(≤ 5 MB)")
    if not raw:
        raise HTTPException(400, "檔案為空")
    # 副檔名(從 content-type 推;沒推到就用 jpg)
    ct = file.content_type or "image/jpeg"
    ext = ct.split("/")[-1].split(";")[0].strip().lower()
    if ext == "jpeg":
        ext = "jpg"
    if ext not in {"jpg", "png", "webp", "gif"}:
        ext = "jpg"
    key = f"avatars/{user.username}/{uuid.uuid4().hex}.{ext}"
    url = save_bytes(raw, key, bucket="pic", content_type=ct)
    user.avatar_url = url
    await db.flush()
    await db.refresh(user)
    return user


@router.delete("/auth/me/avatar", response_model=UserResponse, tags=["U · 認證"])
async def remove_avatar(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """清掉大頭貼(實體檔留在 SeaweedFS,只清欄位)。"""
    user.avatar_url = None
    await db.flush()
    await db.refresh(user)
    return user


@router.post("/auth/change-password", tags=["U · 認證"])
async def change_password(
    payload: ChangePasswordRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not verify_password(payload.old_password, user.password_hash):
        raise HTTPException(400, "目前密碼不正確")
    if not payload.new_password or len(payload.new_password) < 6:
        raise HTTPException(400, "新密碼至少 6 字元")
    user.password_hash = hash_password(payload.new_password)
    await db.flush()
    return {"ok": True}


# ── 使用者管理（需 superuser） ──────────────────────────────────────

def _require_superuser(user: User) -> None:
    if not user.is_superuser:
        raise HTTPException(403, "需要 superuser 權限")


@router.get("/auth/users", response_model=list[UserResponse], tags=["U · 認證"])
async def list_users(
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    _require_superuser(user)
    rows = (await db.execute(select(User).order_by(User.username))).scalars().all()
    return list(rows)


@router.get("/auth/users/assignable", response_model=list[UserResponse], tags=["U · 認證"])
async def list_assignable_users(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """列同 organization 內 is_active=True 的使用者,給「指派任務」下拉用。

    任何登入者都可以呼叫(不需 superuser);用 organization_id 做硬隔離,
    superuser 看得到全部使用者。
    """
    stmt = select(User).where(User.is_active.is_(True)).order_by(User.username)
    if not user.is_superuser:
        stmt = stmt.where(User.organization_id == user.organization_id)
    rows = (await db.execute(stmt)).scalars().all()
    return list(rows)


@router.post("/auth/users", response_model=UserResponse, status_code=201, tags=["U · 認證"])
async def create_user(
    payload: UserCreateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _require_superuser(user)
    if not payload.username or not payload.password:
        raise HTTPException(400, "帳號 / 密碼必填")
    if len(payload.password) < 6:
        raise HTTPException(400, "密碼至少 6 字元")
    existing = (
        await db.execute(select(User).where(User.username == payload.username))
    ).scalar_one_or_none()
    if existing:
        raise HTTPException(409, f"帳號「{payload.username}」已存在")
    if payload.role_id:
        role = await db.get(Role, payload.role_id)
        if not role:
            raise HTTPException(400, "role_id 不存在")
    new_user = User(
        username=payload.username,
        display_name=payload.display_name,
        email=payload.email,
        password_hash=hash_password(payload.password),
        role_id=payload.role_id,
        # 沒指定 → 跟建立者同 org（普通 admin 不能跨 org 開使用者）
        organization_id=payload.organization_id or user.organization_id,
        is_superuser=payload.is_superuser,
    )
    db.add(new_user)
    await db.flush()
    await db.refresh(new_user)
    return new_user


@router.delete("/auth/users/{username}", status_code=204, tags=["U · 認證"])
async def delete_user(
    username: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _require_superuser(user)
    if username == user.username:
        raise HTTPException(400, "不能刪除自己")
    target = (
        await db.execute(select(User).where(User.username == username))
    ).scalar_one_or_none()
    if not target:
        raise HTTPException(404, "使用者不存在")
    await db.delete(target)
    await db.flush()
