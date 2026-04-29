"""Auth REST endpoints — 登入 / 登出 / 取得當前使用者 / 變更密碼 / 管理使用者。

注意：此檔不寫 `from __future__ import annotations`；slowapi 的 @limiter.limit
裝飾器會讀取 function signature 做型別內省，搭配延後求值的 forward-ref
（如 `payload: LoginRequest`）會在 FastAPI 註冊路由時拋
`PydanticUndefinedAnnotation`。
"""
from datetime import datetime
from typing import Optional

import io
import re
import uuid

import jwt as pyjwt
from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from sqlalchemy import select
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
    ChangePasswordRequest,
    LoginRequest,
    RefreshRequest,
    RegisterRequest,
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
