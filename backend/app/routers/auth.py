"""Auth REST endpoints — 登入 / 登出 / 取得當前使用者 / 變更密碼 / 管理使用者。

註冊政策:本系統**禁止自助註冊**。使用者帳號統一由管理員透過
``POST /auth/users`` 建立。歷史上的 ``/auth/register`` /
``/auth/redeem-invite`` / ``/auth/request-access`` / ``/auth/bootstrap-invite``
路徑已全部下架,只保留 ``/auth/register`` 一個 410 Gone 的 stub 以提示
舊 client。

注意：此檔不寫 `from __future__ import annotations`；slowapi 的 @limiter.limit
裝飾器會讀取 function signature 做型別內省，搭配延後求值的 forward-ref
（如 `payload: LoginRequest`）會在 FastAPI 註冊路由時拋
`PydanticUndefinedAnnotation`。
"""
import secrets
import uuid
from datetime import datetime, timedelta
from typing import Optional

import jwt as pyjwt
from fastapi import APIRouter, Depends, File, HTTPException, Request, Response, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.storage_service import save_bytes

from app.auth.dependencies import get_current_user
from app.auth.revocation import revoke as revoke_jti
from app.auth.security import (
    ACCESS_TOKEN_TTL_MINUTES,
    ACTIVE_ORG_COOKIE_NAME,
    ACTIVE_ORG_COOKIE_TTL_DAYS,
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    sign_active_org_cookie,
    verify_password,
)
from app.rate_limit import limiter
from app.database import get_db
from app.models.org_membership import OrgMembership
from app.models.organization import Organization
from app.models.password_reset_token import PasswordResetToken
from app.models.role import Role
from app.models.user import User
from app.schemas.auth import (
    ChangePasswordRequest,
    ForgotPasswordRequest,
    ForgotPasswordResponse,
    LoginRequest,
    RefreshRequest,
    ResetPasswordRequest,
    ResetPasswordTokenInfo,
    TokenResponse,
    UserAdminUpdateRequest,
    UserCreateRequest,
    UserResetPasswordRequest,
    UserResponse,
    UserUpdateMeRequest,
)


# 重置 token 有效期。1 小時是常見、夠寬,且夠短不會堆積過多 dormant token。
# Phase 5 cutover 後實際上沒有 endpoint 在用,留著只為向下相容(operator 想
# rollback 時不需要重灌 schema)。
PASSWORD_RESET_TTL_HOURS = 1

router = APIRouter()


# ── Phase 5: 已下架端點共用回應 ──────────────────────────────────────────
# Casdoor 接管後,本地密碼登入 / 改密 / 重設密碼 / 管理員建/改/刪使用者 /
# 自訂角色等行為都搬到 Casdoor admin UI(http://<host>/casdoor/)。route
# handler 保留只為了讓 OpenAPI 文件仍能看到「為什麼這個 endpoint 不見了」。

def _gone(code: str, moved_to: str) -> HTTPException:
    return HTTPException(
        status_code=410,
        detail={
            "code": code,
            "message": "本端點已下架,請改用 Casdoor",
            "moved_to": moved_to,
        },
    )


@router.post("/auth/login", status_code=410, tags=["U · 認證"])
async def login_disabled(request: Request) -> dict:
    """密碼登入已下架(Phase 5 cutover)— 一律走 ``GET /api/auth/casdoor/login``。

    SPA 端的「使用 Casdoor 登入」按鈕會把 ``window.location`` 直接跳到該入口。
    保留 410 stub 讓舊 client 看見明確錯誤訊息,而不是 404。
    """
    raise _gone("password_login_disabled", "/api/auth/casdoor/login")


@router.post("/auth/register", status_code=410, tags=["U · 認證"])
async def register_disabled(request: Request) -> dict:
    """自助註冊已停用。

    本系統改為「管理員建立帳號」單一管道,所有使用者一律由 admin 透過
    `POST /auth/users` 建立。舊的 invite-code / email-domain 自動歸屬流程
    一併移除,避免任意人寫入 default org 的安全風險。

    保留此 stub 是為了:
      * 給舊的 client 一個明確的 410 + JSON 訊息,而不是 404
      * 在 OpenAPI 文件留一行紀錄,方便讀者知道功能搬到哪裡
    """
    raise HTTPException(
        status_code=410,
        detail={
            "code": "registration_disabled",
            "message": "自助註冊已停用,請聯絡管理員開設帳號",
        },
    )


# ── 忘記密碼:三步流程 ────────────────────────────────────────────────────
#   1. POST /auth/forgot-password          (匿名;rate-limited)
#   2. GET  /auth/reset-password/check     (匿名;前端載入時預檢 token)
#   3. POST /auth/reset-password           (匿名;帶 token + new_password)
#
# Privacy:forgot-password 永遠回 200 + 通用訊息,不洩露 username/email
# 是否存在;只有寄信端會 silently no-op。reset-password 真實驗證才會回錯。

@router.post(
    "/auth/forgot-password",
    response_model=ForgotPasswordResponse,
    tags=["U · 認證"],
)
async def forgot_password() -> dict:
    """已下架(Phase 5)— Casdoor 自帶忘記密碼流程,前端按鈕改連到
    ``/casdoor/forget/<app>``。"""
    raise _gone("forgot_password_disabled", "/casdoor/forget/rl-platform")


@router.get("/auth/reset-password/check", status_code=410, tags=["U · 認證"])
async def check_reset_token_disabled() -> dict:
    raise _gone("forgot_password_disabled", "/casdoor/forget/rl-platform")


@router.post("/auth/reset-password", status_code=410, tags=["U · 認證"])
async def reset_password_disabled() -> dict:
    raise _gone("forgot_password_disabled", "/casdoor/forget/rl-platform")


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


@router.get("/auth/me/orgs", tags=["U · 認證"])
async def my_orgs(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """列出 current_user 屬於哪些組織。前端 org switcher 用。

    每筆 row:
    * `organization_id` / `slug` / `name`
    * `role_id` / `role_name`(在這個 org 裡的角色;NULL = 沒指派)
    * `is_default`(OrgMembership.is_default;同 user 最多一個 row 為 true)
    * `is_current`(等同 organization_id == user.organization_id;標示前端當前 active org)
    """
    rows = (
        await db.execute(
            select(OrgMembership, Organization, Role)
            .join(Organization, Organization.id == OrgMembership.organization_id)
            .outerjoin(Role, Role.id == OrgMembership.role_id)
            .where(OrgMembership.username == user.username)
            .where(OrgMembership.status == "active")
            .order_by(Organization.name)
        )
    ).all()
    return [
        {
            "organization_id": org.id,
            "slug": org.slug,
            "name": org.name,
            "role_id": role.id if role else None,
            "role_name": role.name if role else None,
            "is_default": bool(mem.is_default),
            "is_current": org.id == user.organization_id,
        }
        for mem, org, role in rows
    ]


@router.post("/auth/switch-org", response_model=TokenResponse, tags=["U · 認證"])
async def switch_org(
    request: Request,
    payload: dict,
    response: Response,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """切換 active 組織。要求:
    1. body `{"organization_id": "..."}`
    2. current_user 在該 org 必須有 OrgMembership(active 狀態)
    通過 → 更新 `users.organization_id` + 重新簽 access_token(payload.org_id 變更)。
    refresh_token 不簽,沿用原本的(下次過期才重簽)。
    """
    target_org_id = (payload or {}).get("organization_id")
    if not target_org_id:
        raise HTTPException(400, "缺少 organization_id")
    mem = (
        await db.execute(
            select(OrgMembership)
            .where(OrgMembership.username == user.username)
            .where(OrgMembership.organization_id == target_org_id)
            .where(OrgMembership.status == "active")
        )
    ).scalar_one_or_none()
    if not mem and not user.is_superuser:
        raise HTTPException(403, "您不是該組織的成員")
    # superuser 可切到任何 org(維持既有行為);仍需檢查 org 存在
    if not mem:
        org_exists = (
            await db.execute(select(Organization).where(Organization.id == target_org_id))
        ).scalar_one_or_none()
        if not org_exists:
            raise HTTPException(404, "找不到該組織")
    user.organization_id = target_org_id
    await db.flush()
    extra = {"org_id": target_org_id, "is_superuser": user.is_superuser}
    # 同時設定 active_org_id 簽章 cookie:Phase 4 拔掉 JWT.org_id 後,middleware
    # 還能繼續從 cookie 拿到 active org 而不破壞既有 SPA 行為。
    response.set_cookie(
        key=ACTIVE_ORG_COOKIE_NAME,
        value=sign_active_org_cookie(user.username, target_org_id),
        max_age=ACTIVE_ORG_COOKIE_TTL_DAYS * 24 * 3600,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
    )
    return TokenResponse(
        access_token=create_access_token(user.username, extra=extra),
        refresh_token=create_refresh_token(user.username),
        expires_in=ACCESS_TOKEN_TTL_MINUTES * 60,
    )


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


@router.post("/auth/change-password", status_code=410, tags=["U · 認證"])
async def change_password_disabled() -> dict:
    """已下架(Phase 5)— 使用者自助改密碼一律進 Casdoor 個人設定頁。"""
    raise _gone("change_password_disabled", "/casdoor/account")


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


# ── 使用者管理寫入端點全部下架(Phase 5)──────────────────────────────
# 建立 / 修改 / 重設密碼 / 刪除使用者一律到 Casdoor admin UI 操作;backend
# 仍保留 GET list / GET assignable(只讀,供「指派任務 / 加入專案成員」UI 用)。
# Webhook(Phase 6)會把 Casdoor 的使用者異動同步到本地 users 表。

@router.post("/auth/users", status_code=410, tags=["U · 認證"])
async def create_user_disabled() -> dict:
    raise _gone("user_create_disabled", "/casdoor/users")


@router.put("/auth/users/{username}", status_code=410, tags=["U · 認證"])
async def admin_update_user_disabled(username: str) -> dict:
    raise _gone("user_update_disabled", f"/casdoor/users/autotest/{username}")


@router.post("/auth/users/{username}/reset-password", status_code=410, tags=["U · 認證"])
async def admin_reset_password_disabled(username: str) -> dict:
    raise _gone(
        "user_reset_password_disabled",
        f"/casdoor/users/autotest/{username}",
    )


@router.delete("/auth/users/{username}", status_code=410, tags=["U · 認證"])
async def delete_user_disabled(username: str) -> dict:
    raise _gone("user_delete_disabled", f"/casdoor/users/autotest/{username}")
