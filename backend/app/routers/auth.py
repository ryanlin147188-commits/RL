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
PASSWORD_RESET_TTL_HOURS = 1

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
        must_change_password=bool(user.must_change_password),
    )


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
@limiter.limit("3/hour")  # 同 IP 每小時最多 3 次,避免被當寄信跳板
async def forgot_password(
    request: Request,
    payload: ForgotPasswordRequest,
    db: AsyncSession = Depends(get_db),
) -> ForgotPasswordResponse:
    """寄重置連結到使用者 email。

    成功與否(帳號存在 / 已停用 / email 不符 / 寄信失敗)在 HTTP 回應中
    一律呈現為 ``200 + {"sent": true}``;真正的執行結果寫進 server log。
    """
    import logging
    logger = logging.getLogger(__name__)

    uname = (payload.username or "").strip()
    email = (payload.email or "").strip().lower()
    if not uname or not email or "@" not in email:
        # 格式錯誤直接回通用訊息(同樣不洩露)
        return ForgotPasswordResponse()

    user = (
        await db.execute(select(User).where(User.username == uname))
    ).scalar_one_or_none()
    if not user or not user.is_active:
        logger.info("forgot-password: no active user '%s'", uname)
        return ForgotPasswordResponse()
    if (user.email or "").strip().lower() != email:
        logger.info("forgot-password: email mismatch for user '%s'", uname)
        return ForgotPasswordResponse()

    # mint token
    token_value = secrets.token_urlsafe(32)
    expires_at = datetime.utcnow() + timedelta(hours=PASSWORD_RESET_TTL_HOURS)
    client_ip = request.client.host if request.client else None
    prt = PasswordResetToken(
        token=token_value,
        username=user.username,
        email_sent_to=email,
        expires_at=expires_at,
        requested_ip=client_ip,
    )
    db.add(prt)
    await db.flush()

    # 寄信(透過 Celery,失敗不影響本端回應)
    try:
        from app.services.email_service import render_password_reset_email
        from tasks.email_tasks import send_email_task

        # reset URL:同 host + 前端會讀 ?reset_token=...
        reset_url = (
            f"{request.url.scheme}://{request.url.netloc}/?reset_token={token_value}"
        )
        html_body, text_body = render_password_reset_email(
            display_name=user.display_name or user.username,
            reset_url=reset_url,
            expires_at=expires_at.strftime("%Y-%m-%d %H:%M UTC"),
        )
        send_email_task.delay(
            to=email,
            subject="AutoTest 密碼重置連結",
            html_body=html_body,
            text_body=text_body,
            organization_id=user.organization_id,
        )
        logger.info(
            "forgot-password: token=%s issued for user=%s ip=%s",
            token_value[:8] + "...", uname, client_ip,
        )
    except Exception:  # noqa: BLE001
        logger.exception(
            "forgot-password: token saved but email enqueue failed for %s", uname,
        )

    return ForgotPasswordResponse()


@router.get(
    "/auth/reset-password/check",
    response_model=ResetPasswordTokenInfo,
    tags=["U · 認證"],
)
async def check_reset_token(
    token: str,
    db: AsyncSession = Depends(get_db),
) -> ResetPasswordTokenInfo:
    """前端載入「設定新密碼」表單前先 hit 這支驗 token,避免使用者輸入完密碼
    才被告知過期。回傳 ``valid`` + ``expires_at``;不洩露 username。"""
    if not token:
        return ResetPasswordTokenInfo(valid=False)
    prt = (
        await db.execute(
            select(PasswordResetToken).where(PasswordResetToken.token == token)
        )
    ).scalar_one_or_none()
    if not prt or prt.used_at is not None or prt.expires_at < datetime.utcnow():
        return ResetPasswordTokenInfo(valid=False)
    return ResetPasswordTokenInfo(valid=True, expires_at=prt.expires_at)


@router.post(
    "/auth/reset-password",
    tags=["U · 認證"],
)
@limiter.limit("10/hour")  # 同 IP 每小時最多 10 次嘗試,避免暴力 token 猜測
async def reset_password(
    request: Request,
    payload: ResetPasswordRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """以 forgot-password 寄出的 token 設定新密碼。

    成功 → 用新密碼覆蓋 password_hash + 標記 token used + 把 user 的
    must_change_password 設 False(代表使用者已自主修改,不需再 force)。
    """
    if not payload.token:
        raise HTTPException(400, "缺少 token")
    if not payload.new_password or len(payload.new_password) < 6:
        raise HTTPException(400, "新密碼至少 6 字元")

    prt = (
        await db.execute(
            select(PasswordResetToken).where(PasswordResetToken.token == payload.token)
        )
    ).scalar_one_or_none()
    if not prt:
        raise HTTPException(400, "重置連結無效或已被使用")
    if prt.used_at is not None:
        raise HTTPException(400, "重置連結已被使用,請重新發起忘記密碼")
    if prt.expires_at < datetime.utcnow():
        raise HTTPException(400, "重置連結已過期,請重新發起忘記密碼")

    user = (
        await db.execute(select(User).where(User.username == prt.username))
    ).scalar_one_or_none()
    if not user or not user.is_active:
        # token 有效但帳號已被停用 / 刪除 — 仍然 mark used,避免被重試
        prt.used_at = datetime.utcnow()
        await db.flush()
        raise HTTPException(400, "帳號已停用,請聯絡管理員")

    user.password_hash = hash_password(payload.new_password)
    user.must_change_password = False
    prt.used_at = datetime.utcnow()
    # 同 user 還未使用的其他 token 一併失效(避免外洩 token 又被用)
    other_active = (
        await db.execute(
            select(PasswordResetToken)
            .where(PasswordResetToken.username == user.username)
            .where(PasswordResetToken.id != prt.id)
            .where(PasswordResetToken.used_at.is_(None))
        )
    ).scalars().all()
    for t in other_active:
        t.used_at = datetime.utcnow()
    await db.flush()

    return {"ok": True, "username": user.username}


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
    通過 → 更新 `users.organization_id` + 重新簽 access_token(payload.org_id 變更)
    + 設 ``active_org_id`` 簽章 cookie(middleware 偏好讀此 cookie 後再 fall back
    到 JWT.org_id;為了讓 Casbin enforcer 拿到準確的 domain)。
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
    response.set_cookie(
        key=ACTIVE_ORG_COOKIE_NAME,
        value=sign_active_org_cookie(user.username, target_org_id),
        max_age=ACTIVE_ORG_COOKIE_TTL_DAYS * 24 * 3600,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
    )
    extra = {"org_id": target_org_id, "is_superuser": user.is_superuser}
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
    if payload.new_password == payload.old_password:
        raise HTTPException(400, "新密碼不可與目前密碼相同")
    user.password_hash = hash_password(payload.new_password)
    # 走完強制改密碼流程後解閘,後續 API 才能正常呼叫
    user.must_change_password = False
    await db.flush()
    return {"ok": True}


# ── v1.1.6 首登 profile setup(三欄位一次提交) ──────────────────────────
# 觸發條件:`users.must_change_password=True`(seed admin / admin 建出來的
# 新 user / admin reset password 三條路徑都會把 flag 設成 True)。
# 三個欄位一次寫:display_name / email / new_password,完成後 flag 解開,
# 後續 API 才能正常呼叫(``get_current_user`` 內的閘門邏輯不變)。

@router.post("/auth/profile-setup", response_model=UserResponse, tags=["U · 認證"])
async def profile_setup(
    payload: dict,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """首登 modal 用 — 一次補齊 display_name / email / 改密碼。

    body::

        {
          "display_name": "Alice",       # 必填,1-120 字元
          "email": "alice@example.com",  # 必填(可改但不能空)
          "new_password": "newpw123"     # 必填,≥ 6 字元
        }

    跟 ``/auth/change-password`` 的差異:
    * 不需要 ``old_password``(SSO JIT 進來的 user 不知道自己的初始隨機 hash)
    * 一次寫三個欄位,UX 上是「首登流程」單一動作
    * 沒驗證 ``new_password != old_password``,因為使用者可能本來就沒密碼
    """
    display_name = (payload or {}).get("display_name", "")
    email = (payload or {}).get("email", "")
    new_password = (payload or {}).get("new_password", "")

    display_name = display_name.strip() if isinstance(display_name, str) else ""
    email = email.strip().lower() if isinstance(email, str) else ""
    new_password = new_password if isinstance(new_password, str) else ""

    if not display_name or len(display_name) > 120:
        raise HTTPException(400, "顯示名稱必填,長度 1-120 字元")
    if not email or "@" not in email or len(email) > 255:
        raise HTTPException(400, "email 必填且需有效格式")
    if not new_password or len(new_password) < 6:
        raise HTTPException(400, "新密碼至少 6 字元")

    user.display_name = display_name
    user.email = email
    user.password_hash = hash_password(new_password)
    user.must_change_password = False
    await db.flush()
    await db.refresh(user)
    return user


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
        # v1.1.6:即使 admin 給了 password,新 user 仍要走首登 profile-setup
        # modal 一次補齊 display_name / email / 改自己的密碼。
        must_change_password=True,
    )
    db.add(new_user)
    await db.flush()
    # 多組織模型:同步加 OrgMembership(預設這就是該 user 的 active org)。
    if new_user.organization_id:
        db.add(OrgMembership(
            username=new_user.username,
            organization_id=new_user.organization_id,
            role_id=new_user.role_id,
            is_default=True,
            status="active",
            invited_by=user.username,
        ))
        await db.flush()
    await db.refresh(new_user)
    from app.auth.casbin_sync import schedule_user_resync
    schedule_user_resync(new_user.username)
    return new_user


@router.put("/auth/users/{username}", response_model=UserResponse, tags=["U · 認證"])
async def admin_update_user(
    username: str,
    payload: UserAdminUpdateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Superuser 修改其他使用者的基本資料 / 角色 / 啟用狀態 / superuser 旗標。

    密碼不在這裡改,改密碼走 ``POST /auth/users/{username}/reset-password``。
    """
    _require_superuser(user)
    target = (
        await db.execute(select(User).where(User.username == username))
    ).scalar_one_or_none()
    if not target:
        raise HTTPException(404, "使用者不存在")
    if payload.display_name is not None:
        target.display_name = payload.display_name.strip()[:120] or None
    if payload.email is not None:
        target.email = payload.email.strip()[:255] or None
    if payload.role_id is not None:
        if payload.role_id:
            role = await db.get(Role, payload.role_id)
            if not role:
                raise HTTPException(404, "找不到該角色")
            target.role_id = payload.role_id
        else:
            target.role_id = None
    if payload.is_active is not None:
        if target.username == user.username and not payload.is_active:
            raise HTTPException(400, "不能停用自己")
        target.is_active = bool(payload.is_active)
    if payload.is_superuser is not None:
        if target.username == user.username and not payload.is_superuser:
            raise HTTPException(400, "不能撤銷自己的 superuser 權限")
        target.is_superuser = bool(payload.is_superuser)
    await db.flush()
    await db.refresh(target)
    from app.auth.casbin_sync import schedule_user_resync
    schedule_user_resync(target.username)
    return target


@router.post("/auth/users/{username}/reset-password", tags=["U · 認證"])
async def admin_reset_password(
    username: str,
    payload: UserResetPasswordRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Superuser 為其他使用者強制重設密碼。

    重設後 ``must_change_password`` 一律設為 True,使用者下次登入會被前端
    擋下並要求自行改密碼,管理員不會看到使用者的最終密碼。
    """
    _require_superuser(user)
    if not payload.new_password or len(payload.new_password) < 6:
        raise HTTPException(400, "新密碼至少 6 字元")
    target = (
        await db.execute(select(User).where(User.username == username))
    ).scalar_one_or_none()
    if not target:
        raise HTTPException(404, "使用者不存在")
    target.password_hash = hash_password(payload.new_password)
    target.must_change_password = True
    await db.flush()
    return {"ok": True, "must_change_password": True}


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
    from app.auth.casbin_sync import schedule_user_resync
    schedule_user_resync(username)
