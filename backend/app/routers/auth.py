"""Auth REST endpoints — 登入 / 登出 / 取得當前使用者 / 變更密碼 / 管理使用者。

注意：此檔不寫 `from __future__ import annotations`；slowapi 的 @limiter.limit
裝飾器會讀取 function signature 做型別內省，搭配延後求值的 forward-ref
（如 `payload: LoginRequest`）會在 FastAPI 註冊路由時拋
`PydanticUndefinedAnnotation`。
"""
from datetime import datetime
from typing import Optional

import jwt as pyjwt
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
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
from app.models.role import Role
from app.models.user import User
from app.schemas.auth import (
    ChangePasswordRequest,
    LoginRequest,
    RefreshRequest,
    TokenResponse,
    UserCreateRequest,
    UserResponse,
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


@router.get("/auth/me", response_model=UserResponse, tags=["U · 認證"])
async def me(user: User = Depends(get_current_user)):
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
