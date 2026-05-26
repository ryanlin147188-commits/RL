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
from app.auth.fastapi_users_integration import (
    UserManager,
    get_jwt_strategy,
    get_user_manager,
)
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
from app.models.email_verification_token import EmailVerificationToken
from app.models.org_membership import OrgMembership
from app.models.organization import Organization
from app.models.password_reset_token import PasswordResetToken
from app.models.project import Project
from app.models.project_member import ProjectMember
from app.models.role import Role
from app.models.user import User
from app.schemas.auth import (
    ChangePasswordRequest,
    ForgotPasswordRequest,
    ForgotPasswordResponse,
    LoginRequest,
    RefreshRequest,
    RegisterRequest,
    RegisterResponse,
    ResendVerifyRequest,
    ResetPasswordRequest,
    ResetPasswordTokenInfo,
    TokenResponse,
    UserAdminUpdateRequest,
    UserCreateRequest,
    UserResetPasswordRequest,
    UserResponse,
    UserUpdateMeRequest,
    VerifyTokenRequest,
    VerifyTokenResponse,
)


# 重置 token 有效期。1 小時是常見、夠寬,且夠短不會堆積過多 dormant token。
PASSWORD_RESET_TTL_HOURS = 1
# 註冊驗證 token TTL — email 可能有延遲,所以給 24 小時(比 reset 1 小時長)
EMAIL_VERIFY_TTL_HOURS = 24

router = APIRouter()


@router.post("/auth/login", response_model=TokenResponse, tags=["U · 認證"])
# v1.1.10:gateway routes.yaml 主擋 100/minute,backend 這層放寬到 200/minute
# 只擋「繞過 gateway 直打 backend」的攻擊。日常瀏覽器流量被 gateway 先攔。
@limiter.limit("200/minute")
async def login(
    request: Request,
    payload: LoginRequest,
    user_manager: UserManager = Depends(get_user_manager),
):
    """v1.1.8:整支走 fastapi-users。

    - ``UserManager.authenticate_by_username`` 包了 username lookup + bcrypt
      verify + constant-time dummy hash(防 timing attack)+ argon2 progressive
      rehash。
    - ``JWTStrategy.write_token`` 簽 access token,內部 claim format 對齊
      v1.1.7 之前的(sub=username, org_id, is_superuser),SPA / Casbin 不變。
    - refresh token 維持手刻(fastapi-users 13 沒有 refresh 概念);
      ``must_change_password`` 跟 ``user`` object 也手動補進 response 給 SPA。
    """
    user = await user_manager.authenticate_by_username(
        payload.username, payload.password
    )
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="帳號或密碼錯誤")
    # on_after_login hook(寫 last_login_at)
    await user_manager.on_after_login(user, request=request)
    access_token = await get_jwt_strategy().write_token(user)
    return TokenResponse(
        access_token=access_token,
        refresh_token=create_refresh_token(user.username),
        expires_in=ACCESS_TOKEN_TTL_MINUTES * 60,
        must_change_password=bool(user.must_change_password),
    )


# ── 自助註冊(v1.1.10 簡化版)─────────────────────────────────
# 流程:
#   POST /auth/register  匿名;建 user(is_active=True,可立即登入)
#                        + 自動建個人 Organization + OrgMembership(admin role)
#
# 設計決策:取消 email 驗證流程(user 反映太繁瑣)。Email 驗證只保留:
#   1. 忘記密碼(/auth/forgot-password)
#   2. 加入別人專案協作(ProjectInvite — 邀請信寄 email,redeem 時驗 email)
#
# 下面的 /auth/register/verify-check, /verify, /resend-verify endpoint 保留
# 但目前路徑不會觸發 — 之後若要重啟用 email 驗證 / email change confirm 可直接用。
#
# Privacy:同 forgot-password 模式 — username 衝突回 409(無法 silent),但
# email 衝突 silent 不洩漏。

@router.post(
    "/auth/register",
    response_model=RegisterResponse,
    status_code=201,
    tags=["U · 認證"],
)
@limiter.limit("10/hour")  # 同 IP 每小時最多 10 次,防爬蟲創帳號
async def register(
    request: Request,
    payload: RegisterRequest,
    user_manager: UserManager = Depends(get_user_manager),
    db: AsyncSession = Depends(get_db),
) -> RegisterResponse:
    """匿名 user 自助註冊(v1.1.10 簡化版)。

    建 ``users`` row 並直接 ``is_active=True``。同時自動建立:
    * 個人 Organization(``slug=personal-{username}``, ``name="{display_name} 的工作空間"``)
    * OrgMembership(``is_default=True``、套用 system "admin" role)
    * ``users.organization_id`` / ``users.role_id`` 直接指向上面那組

    註冊完即可登入,在自己的工作空間是管理員。若要協作別人的專案,走
    ``ProjectInvite``(寄信 + 點連結兌換,email 必須相符)。
    """
    import logging
    logger = logging.getLogger(__name__)

    username = payload.username.strip()
    email = payload.email.strip().lower()
    display_name = payload.display_name.strip()

    if "@" not in email:
        raise HTTPException(422, "Email 格式錯誤")

    # username 衝突 → 直接告知(無法 silent,user 必須改名)
    existing_username = (
        await db.execute(select(User).where(User.username == username))
    ).scalar_one_or_none()
    if existing_username:
        raise HTTPException(409, {"code": "username_taken", "message": "帳號已被使用"})

    # email 衝突 → silent(避免帳號探測攻擊)
    # 不告訴 caller,直接回成功但 row 不建,也不寄信。
    existing_email = (
        await db.execute(select(User).where(User.email == email))
    ).scalar_one_or_none()
    if existing_email:
        logger.info("register: email '%s' already taken (silent)", email)
        return RegisterResponse(message="註冊完成,請以您的帳號登入")

    # 找 system "admin" role(_seed_default_roles 啟動時建好的全 70 權限)。
    # 注意:_seed_default_org_and_backfill 會把 NULL organization_id 全部回填到
    # default org,所以不能用 organization_id IS NULL 過濾,只匹配 name+is_system。
    admin_role = (
        await db.execute(
            select(Role).where(Role.name == "admin", Role.is_system.is_(True))
        )
    ).scalar_one_or_none()
    if not admin_role:
        logger.error("register: system admin role not seeded; abort")
        raise HTTPException(500, "系統未完成初始化,請聯絡管理員")

    # 1) 建個人 Organization
    # slug:username 已 unique(/^[A-Za-z0-9_.\-]+$/)→ slug 也必 unique
    org_slug = f"personal-{username.lower()}"
    org_name = f"{display_name} 的工作空間"
    new_org = Organization(
        id=str(uuid.uuid4()),
        slug=org_slug,
        name=org_name,
        plan="free",
    )
    db.add(new_org)
    await db.flush()

    # 2) 建 User(is_active=True 直接可用)+ 指向個人 org + admin role
    new_user = User(
        id=str(uuid.uuid4()),
        username=username,
        email=email,
        display_name=display_name,
        password_hash=hash_password(payload.password),
        is_active=True,                  # 不再驗證 email
        is_superuser=False,
        must_change_password=False,
        organization_id=new_org.id,
        role_id=admin_role.id,
    )
    db.add(new_user)
    await db.flush()

    # 3) OrgMembership — 個人 org 內的 admin,設成 default
    db.add(OrgMembership(
        username=new_user.username,
        user_id=new_user.id,
        organization_id=new_org.id,
        role_id=admin_role.id,
        is_default=True,
        status="active",
        invited_by=None,
    ))
    await db.commit()

    client_ip = request.client.host if request.client else None
    logger.info(
        "register: created user=%s org=%s(slug=%s) ip=%s",
        username, new_org.id, org_slug, client_ip,
    )

    # 4) 觸發 Casbin 重新同步該 user 的 policy
    try:
        from app.auth.casbin_sync import schedule_user_resync
        schedule_user_resync(new_user.username)
    except Exception:  # noqa: BLE001
        logger.exception("register: casbin resync schedule failed for %s", username)

    return RegisterResponse(message="註冊成功,請以您的帳號密碼登入")


# ── 以下三個 verify endpoint 在 v1.1.10 簡化後不會被新註冊流程觸發 ─────
# 保留:之後若要重啟用 email 驗證,或做 email change confirm 可直接重用。
# 安全:既有 email_verification_tokens 表還在,middleware 白名單也保留;
# 從外部直接呼叫不影響註冊路徑,只能對殘留的 inactive user(歷史資料)生效。

@router.get(
    "/auth/register/verify-check",
    response_model=VerifyTokenResponse,
    tags=["U · 認證"],
    deprecated=True,
)
async def register_verify_check(
    token: str,
    db: AsyncSession = Depends(get_db),
) -> VerifyTokenResponse:
    """[v1.1.10 deprecated] 前端載入 ``/?verify_token=...`` 後先打這支看
    token 還有效嗎。新註冊流程不再寄驗證信,此 endpoint 保留作未來用途。"""
    if not token:
        return VerifyTokenResponse(valid=False)
    evt = (
        await db.execute(
            select(EmailVerificationToken).where(EmailVerificationToken.token == token)
        )
    ).scalar_one_or_none()
    if not evt or evt.used_at is not None or evt.expires_at < datetime.utcnow():
        return VerifyTokenResponse(valid=False)
    return VerifyTokenResponse(
        valid=True, email=evt.email_sent_to, expires_at=evt.expires_at,
    )


@router.post(
    "/auth/register/verify",
    tags=["U · 認證"],
    deprecated=True,
)
@limiter.limit("20/hour")  # 防暴力猜 token
async def register_verify(
    request: Request,
    payload: VerifyTokenRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """[v1.1.10 deprecated] 以 token 啟用對應的 user(``is_active=True``)。
    新註冊流程已直接 is_active=True,此 endpoint 保留作未來用途。"""
    if not payload.token:
        raise HTTPException(400, "缺少 token")

    evt = (
        await db.execute(
            select(EmailVerificationToken).where(EmailVerificationToken.token == payload.token)
        )
    ).scalar_one_or_none()
    if not evt:
        raise HTTPException(400, "驗證連結無效")
    if evt.used_at is not None:
        raise HTTPException(400, "驗證連結已被使用")
    if evt.expires_at < datetime.utcnow():
        raise HTTPException(400, "驗證連結已過期,請重新註冊或請管理員協助")

    user = await db.get(User, evt.user_id)
    if not user:
        evt.used_at = datetime.utcnow()
        await db.flush()
        raise HTTPException(400, "對應帳號已不存在")

    user.is_active = True
    evt.used_at = datetime.utcnow()
    # 同 user 的其他未使用 token 一併失效(避免外洩 token 又被用)
    other_active = (
        await db.execute(
            select(EmailVerificationToken)
            .where(EmailVerificationToken.user_id == user.id)
            .where(EmailVerificationToken.id != evt.id)
            .where(EmailVerificationToken.used_at.is_(None))
        )
    ).scalars().all()
    for t in other_active:
        t.used_at = datetime.utcnow()
    await db.flush()

    return {"ok": True, "username": user.username}


@router.post(
    "/auth/register/resend-verify",
    response_model=RegisterResponse,
    tags=["U · 認證"],
    deprecated=True,
)
@limiter.limit("3/hour")  # 防被當寄信跳板
async def register_resend_verify(
    request: Request,
    payload: ResendVerifyRequest,
    db: AsyncSession = Depends(get_db),
) -> RegisterResponse:
    """[v1.1.10 deprecated] 重寄驗證信。永遠回 200(不洩露 email 存在),失敗 silent。
    新註冊流程不再寄驗證信,此 endpoint 保留作未來用途。"""
    import logging
    logger = logging.getLogger(__name__)

    email = payload.email.strip().lower()
    if "@" not in email:
        return RegisterResponse()

    user = (
        await db.execute(
            select(User)
            .where(User.email == email)
            .where(User.is_active.is_(False))
        )
    ).scalar_one_or_none()
    if not user:
        # 不存在 / 已啟用 → silent
        logger.info("resend-verify: no inactive user for email %s", email)
        return RegisterResponse()

    # 撤掉舊 token,發新的
    from sqlalchemy import delete as sql_delete
    await db.execute(
        sql_delete(EmailVerificationToken).where(EmailVerificationToken.user_id == user.id)
    )
    token_value = secrets.token_urlsafe(32)
    expires_at = datetime.utcnow() + timedelta(hours=EMAIL_VERIFY_TTL_HOURS)
    client_ip = request.client.host if request.client else None
    db.add(EmailVerificationToken(
        token=token_value,
        user_id=user.id,
        email_sent_to=email,
        expires_at=expires_at,
        requested_ip=client_ip,
    ))
    await db.commit()

    try:
        from app.services.email_service import render_registration_verify_email
        from tasks.email_tasks import send_email_task

        verify_url = (
            f"{request.url.scheme}://{request.url.netloc}/?verify_token={token_value}"
        )
        html_body, text_body = render_registration_verify_email(
            display_name=user.display_name or user.username,
            verify_url=verify_url,
            expires_at=expires_at.strftime("%Y-%m-%d %H:%M UTC"),
        )
        send_email_task.delay(
            to=email,
            subject="AutoTest 帳號啟用驗證(重寄)",
            html_body=html_body,
            text_body=text_body,
            organization_id=None,
        )
    except Exception:  # noqa: BLE001
        logger.exception("resend-verify: email enqueue failed for %s", email)

    return RegisterResponse()


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
async def me(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """回傳目前登入使用者基本資料 + ``permissions`` 解析後字串清單,給前端 capability gate。

    * superuser    → ``["*"]`` (萬用,前端 hasPerm 直接 true)
    * 有 role_id   → role.permissions_json 直接複製出來
    * 沒 role_id   → ``[]`` (前端 hasPerm fail-safe deny)
    """
    # v1.1.11:Lazy backfill personal org。`on_after_login` 只在「真的 login」時跑,
    # cookie-based hydrate(_maybeHydrateOidcSession)不會觸發。但前端啟動都會打 me,
    # 在這裡 idempotent 確保 personal org 存在,Ryan.lin 這類既有 user 不必重登就能補上。
    try:
        from app.auth.personal_org import ensure_personal_org
        await ensure_personal_org(db, user, set_as_active=False)
        await db.commit()
    except Exception as e:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning(
            "ensure_personal_org on /me failed for user=%s: %s", user.username, e,
        )
        await db.rollback()
    resp = UserResponse.model_validate(user)
    if user.is_superuser:
        resp.permissions = ["*"]
    elif user.role_id:
        role = await db.get(Role, user.role_id)
        if role and role.permissions_json:
            resp.permissions = list(role.permissions_json)
    return resp


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
    2. current_user 在該 org 必須有 OrgMembership(active 狀態)— 或
       v1.1.11 起放寬:在該 org 內任一 active ProjectMember(跨 org 協作者
       也能切過去當該 org 的 active context;permission 走 ProjectMember.role_id
       或 fallback,不會莫名升權)
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
    # v1.1.11:跨 org 協作者放行 — 在目標 org 內有 active ProjectMember 就 OK。
    if not mem and not user.is_superuser:
        pm_in_target_org = (
            await db.execute(
                select(ProjectMember.id)
                .join(Project, Project.id == ProjectMember.project_id)
                .where(ProjectMember.username == user.username)
                .where(ProjectMember.status == "active")
                .where(Project.organization_id == target_org_id)
                .limit(1)
            )
        ).scalar_one_or_none()
        if not pm_in_target_org:
            raise HTTPException(403, "您不是該組織的成員,也不是任何該組織內專案的成員")
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
    user_manager: UserManager = Depends(get_user_manager),
):
    """v1.1.8 — Admin user create 走 fastapi-users。

    用 ``UserManager.password_helper`` 做密碼 hash(argon2 by default,跟
    PasswordHelper 公用設定),用 ``UserManager.get_by_username`` 做存在性
    檢查。User row 仍由我們建好交給 SQLAlchemy session — fastapi-users 的
    :meth:`UserManager.create` 只支援單一 ``UserCreate`` Pydantic schema,塞
    不下我們的 ``organization_id`` / ``role_id`` / ``is_superuser`` 等欄位,
    所以這支自己組 User 物件,但密碼 hash 路徑走 fastapi-users。
    """
    _require_superuser(user)
    if not payload.username or not payload.password:
        raise HTTPException(400, "帳號 / 密碼必填")
    if len(payload.password) < 6:
        raise HTTPException(400, "密碼至少 6 字元")
    try:
        await user_manager.get_by_username(payload.username)
        raise HTTPException(409, f"帳號「{payload.username}」已存在")
    except Exception as exc:  # noqa: BLE001
        # get_by_username 不存在時會 raise UserNotExists,正是我們要的
        from fastapi_users.exceptions import UserNotExists
        if not isinstance(exc, UserNotExists):
            raise
    if payload.role_id:
        role = await db.get(Role, payload.role_id)
        if not role:
            raise HTTPException(400, "role_id 不存在")
    new_user = User(
        username=payload.username,
        display_name=payload.display_name,
        email=payload.email,
        password_hash=user_manager.password_helper.hash(payload.password),
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
    # on_after_register hook(UserManager 內含)
    await user_manager.on_after_register(new_user)
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
    # v1.1.11:admin POST /auth/users 建出來的 user 也要有 personal org,讓他退完
    # admin org 內所有專案後有地方可回去當 admin。set_as_active=False — admin 是
    # 把他加進 admin 自己的 org,active org 應保持是該 org(set_as_active=True 會
    # hijack 走,違反 admin 預期)。前端 boot-time 會視情況自動切回 personal。
    try:
        from app.auth.personal_org import ensure_personal_org
        await ensure_personal_org(db, new_user, set_as_active=False)
    except Exception as e:  # noqa: BLE001
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "ensure_personal_org for admin-created user=%s failed: %s",
            new_user.username, e,
        )
    await db.refresh(new_user)
    from app.auth.casbin_sync import schedule_user_resync
    schedule_user_resync(new_user.username)

    # ── 寄送註冊歡迎信(transactional;不走 NotificationPreference)──
    # 失敗只記 log,不影響使用者建立流程。
    if new_user.email:
        import logging as _logging
        logger = _logging.getLogger(__name__)
        try:
            from app.services.email_service import render_notification_email
            from tasks.email_tasks import send_email_task
            login_url = "/"  # SPA 進站登入
            title = f"AutoTest 帳號建立成功 — 歡迎 {new_user.display_name or new_user.username}"
            body = (
                f"您好 {new_user.display_name or new_user.username},\n\n"
                f"管理員 {user.username} 已為您建立 AutoTest 帳號:\n"
                f"  使用者名稱:{new_user.username}\n"
                f"  Email:{new_user.email}\n\n"
                f"首次登入請使用管理員提供的密碼,登入後系統會引導您修改密碼。\n"
                f"如有任何問題,請聯絡管理員 {user.username}。"
            )
            html_body, text_body = render_notification_email(
                title=title, body=body, link=login_url,
            )
            send_email_task.delay(
                to=new_user.email,
                subject=title,
                html_body=html_body,
                text_body=text_body,
                organization_id=new_user.organization_id,
            )
            logger.info(
                "register-email: enqueued welcome email for new user=%s",
                new_user.username,
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "register-email: enqueue failed for new user=%s",
                new_user.username,
            )
    return new_user


@router.put("/auth/users/{username}", response_model=UserResponse, tags=["U · 認證"])
async def admin_update_user(
    username: str,
    payload: UserAdminUpdateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    user_manager: UserManager = Depends(get_user_manager),
):
    """v1.1.8 — Admin update 走 fastapi-users。

    target 用 ``UserManager.get_by_username`` 撈,基本資料用
    :meth:`UserManager._update` 套上去(safe=False 讓 admin 能改
    is_superuser / is_active 等 privileged 欄位)。``role_id`` 是我們的擴充
    欄位,fastapi-users 不認識,直接寫 attribute 給 SQLAlchemy session。
    """
    _require_superuser(user)
    from fastapi_users.exceptions import UserNotExists
    try:
        target = await user_manager.get_by_username(username)
    except UserNotExists:
        raise HTTPException(404, "使用者不存在")

    # 防呆:不能停用 / 撤銷自己
    if payload.is_active is False and target.username == user.username:
        raise HTTPException(400, "不能停用自己")
    if payload.is_superuser is False and target.username == user.username:
        raise HTTPException(400, "不能撤銷自己的 superuser 權限")

    # 收 update dict 給 UserManager._update;只把有給值的欄位塞進去。
    update_dict: dict = {}
    if payload.display_name is not None:
        update_dict["display_name"] = payload.display_name.strip()[:120] or None
    if payload.email is not None:
        update_dict["email"] = payload.email.strip()[:255] or None
    if payload.is_active is not None:
        update_dict["is_active"] = bool(payload.is_active)
    if payload.is_superuser is not None:
        update_dict["is_superuser"] = bool(payload.is_superuser)

    # role_id 是擴充欄位,UserManager._update 預設不知道;用 safe=False 跳過
    # 它的 schema validation,直接傳過去,SQLAlchemy 會幫我們 ORM-level 寫入。
    if payload.role_id is not None:
        if payload.role_id:
            role = await db.get(Role, payload.role_id)
            if not role:
                raise HTTPException(404, "找不到該角色")
            update_dict["role_id"] = payload.role_id
        else:
            update_dict["role_id"] = None

    if update_dict:
        # safe=False:admin 可改 is_superuser / is_active 等 privileged 欄位
        await user_manager._update(target, update_dict)

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
    user_manager: UserManager = Depends(get_user_manager),
):
    """v1.1.8 — Reset password 走 fastapi-users PasswordHelper。

    新 hash 用 argon2(PasswordHelper default);``must_change_password=True``
    保證使用者下次登入被擋下,管理員不會看到最終密碼。
    """
    _require_superuser(user)
    if not payload.new_password or len(payload.new_password) < 6:
        raise HTTPException(400, "新密碼至少 6 字元")
    from fastapi_users.exceptions import UserNotExists
    try:
        target = await user_manager.get_by_username(username)
    except UserNotExists:
        raise HTTPException(404, "使用者不存在")
    await user_manager._update(
        target,
        {
            "hashed_password": user_manager.password_helper.hash(payload.new_password),
            "must_change_password": True,
        },
    )
    return {"ok": True, "must_change_password": True}


@router.delete("/auth/users/{username}", status_code=204, tags=["U · 認證"])
async def delete_user(
    username: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    user_manager: UserManager = Depends(get_user_manager),
):
    """v1.1.8 — Delete user 走 ``UserManager.delete``,內含 on_before_delete
    hook(目前我們 UserManager 沒實作 hook,後續想加 audit log 直接 override)。"""
    _require_superuser(user)
    if username == user.username:
        raise HTTPException(400, "不能刪除自己")
    from fastapi_users.exceptions import UserNotExists
    try:
        target = await user_manager.get_by_username(username)
    except UserNotExists:
        raise HTTPException(404, "使用者不存在")
    await user_manager.delete(target)
    from app.auth.casbin_sync import schedule_user_resync
    schedule_user_resync(username)


@router.delete("/auth/me", status_code=204, tags=["U · 認證"])
async def delete_my_account(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    user_manager: UserManager = Depends(get_user_manager),
):
    """使用者自助刪除自己的帳號(v1.1.10)。

    保護:
    * ``is_superuser`` 不允許 self-delete — 避免系統最後一個 admin 自爆。
      要刪 superuser 須由另一個 superuser 走 ``DELETE /auth/users/{u}``,
      或先把該帳號降級。
    * 刪除後 revoke 當前 access token(jti 進 Valkey blocklist);refresh
      token 沒辦法主動 invalidate,但 user row 沒了下次拿 refresh token 換
      access 也會 401。

    Cascade:
    * ``org_memberships`` / ``group_memberships`` / ``api_keys`` /
      ``email_verification_tokens`` 等 FK ``ondelete=CASCADE`` 自動刪除。
    * 若 user 的 active org 是個人 org(``slug='personal-{username}'``)且
      其他 OrgMembership 已被 cascade 刪光,順手把該 Organization 也刪掉,
      避免累積孤兒 org row。其他人共用的 org 不動。
    """
    import logging
    logger = logging.getLogger(__name__)

    if user.is_superuser:
        raise HTTPException(
            400,
            "Superuser 帳號不能自助刪除,請先由另一位 superuser 降級或刪除此帳號",
        )

    username = user.username
    user_id = user.id
    personal_org_id = user.organization_id

    client_ip = request.client.host if request.client else None
    logger.warning(
        "delete_my_account: user=%s id=%s active_org=%s ip=%s",
        username, user_id, personal_org_id, client_ip,
    )

    # 1) 刪 user(會 cascade 掉 OrgMembership / ProjectMember / ApiKey 等)
    await user_manager.delete(user)

    # 2) 如果 active org 是該 user 的個人 org 且現在沒人了,順手清掉。
    #    (註冊時建的 slug 格式:personal-{username.lower()},user 是唯一 member)
    if personal_org_id:
        org = await db.get(Organization, personal_org_id)
        expected_slug = f"personal-{username.lower()}"
        if org and org.slug == expected_slug:
            # 確認沒有其他 OrgMembership / User 還掛在這個 org 上
            still_member = (
                await db.execute(
                    select(OrgMembership).where(
                        OrgMembership.organization_id == personal_org_id
                    ).limit(1)
                )
            ).scalar_one_or_none()
            still_user = (
                await db.execute(
                    select(User).where(User.organization_id == personal_org_id).limit(1)
                )
            ).scalar_one_or_none()
            if not still_member and not still_user:
                await db.delete(org)
                await db.commit()
                logger.info(
                    "delete_my_account: personal org=%s(%s) removed",
                    org.id, expected_slug,
                )

    # 3) Revoke 當前 access token
    payload = getattr(request.state, "user_payload", None) or {}
    await revoke_jti(payload.get("jti"), payload.get("exp"))

    # 4) Casbin policy resync
    from app.auth.casbin_sync import schedule_user_resync
    schedule_user_resync(username)

    return None
