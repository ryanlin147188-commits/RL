"""FastAPI 依賴：從 request.state.user_payload（middleware 注入的 JWT payload）
取出 User ORM 物件，作為 router 參數。

用法：
    @router.get("/me")
    async def me(user: User = Depends(get_current_user)):
        return user
"""
from __future__ import annotations

from typing import Optional

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.user import User


async def get_current_user(
    request: Request, db: AsyncSession = Depends(get_db)
) -> User:
    """強制需要登入；middleware 已驗證 JWT，這裡只是把 username 換成 ORM 物件。"""
    payload = getattr(request.state, "user_payload", None)
    if not payload:
        raise HTTPException(status_code=401, detail="未授權：缺少或無效的 token")
    username = payload.get("sub")
    if not username:
        raise HTTPException(status_code=401, detail="未授權：token 缺少 sub")
    user = (await db.execute(select(User).where(User.username == username))).scalar_one_or_none()
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="使用者不存在或已停用")
    return user


async def get_optional_user(
    request: Request, db: AsyncSession = Depends(get_db)
) -> Optional[User]:
    """允許匿名；有 token 就回 User，沒有就回 None。"""
    payload = getattr(request.state, "user_payload", None)
    if not payload:
        return None
    username = payload.get("sub")
    if not username:
        return None
    user = (await db.execute(select(User).where(User.username == username))).scalar_one_or_none()
    if not user or not user.is_active:
        return None
    return user
