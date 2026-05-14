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


# 當 user.must_change_password=True 時，僅允許這些 path（前綴比對）。
# 涵蓋:取自身資料、改密碼、登出、refresh token。其餘一律 403。
_PASSWORD_RESET_ALLOWED_PATHS = (
    "/api/auth/me",
    "/api/auth/change-password",
    "/api/auth/profile-setup",  # v1.1.6:首登一次性補齊 display_name + email + 改密碼
    "/api/auth/logout",
    "/api/auth/refresh",
)


def _path_is_password_reset_allowed(path: str) -> bool:
    return any(path.startswith(p) for p in _PASSWORD_RESET_ALLOWED_PATHS)


async def get_current_user(
    request: Request, db: AsyncSession = Depends(get_db)
) -> User:
    """強制需要登入；middleware 已驗證 JWT，這裡只是把 username 換成 ORM 物件。

    額外閘門:若 user.must_change_password=True,只放行 _PASSWORD_RESET_ALLOWED_PATHS
    的端點;其他路由一律回 403,前端據此跳出強制改密碼 modal。
    """
    payload = getattr(request.state, "user_payload", None)
    if not payload:
        raise HTTPException(status_code=401, detail="未授權：缺少或無效的 token")
    username = payload.get("sub")
    if not username:
        raise HTTPException(status_code=401, detail="未授權：token 缺少 sub")
    user = (await db.execute(select(User).where(User.username == username))).scalar_one_or_none()
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="使用者不存在或已停用")
    if user.must_change_password and not _path_is_password_reset_allowed(request.url.path):
        raise HTTPException(
            status_code=403,
            detail={
                "code": "must_change_password",
                "message": "首次登入請先修改密碼",
            },
        )
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
