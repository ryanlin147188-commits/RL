"""FastAPI Users 整合層(v1.1.8 — real cutover)。

從 v1.1.7 的「Phase 1 wired but unused」變成**真的**接到 request 路徑:

* :data:`current_active_user` 是 ``Depends(get_current_user)`` 的後繼者。
  底下走 :class:`UsernameSubJWTStrategy` 解 JWT,呼 :class:`UserManager`
  去 DB 撈 User,然後加回我們自家的 ``must_change_password`` gate。
* :func:`UserManager.authenticate_by_username` 是 login endpoint 用的入口,
  comes with constant-time dummy hash for non-existent users(fastapi-users
  做的 timing-attack 防護,比 v1.1.7 之前的手刻路徑更安全)。
* :class:`UsernameSubJWTStrategy` 讓 fastapi-users 用我們既有的 ``sub=username``
  token 格式而不是 UUID id — SPA / Casbin / log 全部繼續用 username 識別。

刻意保留的「不走 fastapi-users」:
* JWT decode 在 middleware 裡(:mod:`app.middleware`)仍走手刻 PyJWT,
  因為 middleware 還要做 token revocation check / ContextVar setup /
  active_org cookie 解析,fastapi-users 沒這些。
* refresh token 走手刻 — fastapi-users 13 沒提供 refresh token 概念。
* Casbin RBAC 完全獨立,fastapi-users 不管授權。
"""
from __future__ import annotations

from typing import Any, AsyncGenerator, Optional

import jwt
from fastapi import Depends, HTTPException, Request
from fastapi_users import BaseUserManager, FastAPIUsers
from fastapi_users import exceptions as fa_exc
from fastapi_users.authentication import (
    AuthenticationBackend,
    BearerTransport,
    JWTStrategy,
)
from fastapi_users.db import SQLAlchemyUserDatabase
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.security import (
    ACCESS_TOKEN_TTL_MINUTES,
    JWT_ALGORITHM,
    JWT_SECRET,
)
from app.database import get_db
from app.models.user import User


# fastapi-users 期望 user.hashed_password / user.is_verified。Phase 4 已做
# attribute-level shim;此檔仍補上以防 import 順序差異 race 出問題。
if not hasattr(User, "hashed_password"):
    def _user_hp_get(self: User) -> str:  # type: ignore[no-redef]
        return self.password_hash

    def _user_hp_set(self: User, value: str) -> None:  # type: ignore[no-redef]
        self.password_hash = value

    User.hashed_password = property(_user_hp_get, _user_hp_set)  # type: ignore[attr-defined]

if not hasattr(User, "is_verified"):
    User.is_verified = property(lambda self: True, lambda self, v: None)  # type: ignore[attr-defined]


# ── User database adapter ─────────────────────────────────────────────

async def get_user_db(
    session: AsyncSession = Depends(get_db),
) -> AsyncGenerator[SQLAlchemyUserDatabase, None]:
    yield SQLAlchemyUserDatabase(session, User)


# ── UserManager ────────────────────────────────────────────────────────

class UserManager(BaseUserManager[User, str]):
    """v1.1.8 真正接 hot path 的 UserManager。

    id 型別仍是 ``str``(JWT sub 是 username);DB PK 是 UUID,但
    auth 流程的對外 identifier 是 username — 兩者解耦,JWT/SPA/Casbin
    完全不必感知 UUID。
    """

    reset_password_token_secret = JWT_SECRET
    verification_token_secret = JWT_SECRET
    reset_password_token_lifetime_seconds = 60 * 60
    verification_token_lifetime_seconds = 60 * 60 * 24

    def parse_id(self, value: Any) -> str:
        if not isinstance(value, str) or not value:
            raise fa_exc.InvalidID()
        return value

    # ─ Username-based lookup ─────────────────────────────────────────
    async def get_by_username(self, username: str) -> User:
        """Lookup by username — login + JWT subject resolution 都走這支。
        non-exist 時 raise :class:`fastapi_users.exceptions.UserNotExists`。"""
        session: AsyncSession = self.user_db.session  # type: ignore[attr-defined]
        row = await session.execute(select(User).where(User.username == username))
        user = row.scalar_one_or_none()
        if user is None:
            raise fa_exc.UserNotExists()
        return user

    async def authenticate_by_username(
        self, username: str, password: str
    ) -> Optional[User]:
        """username + password → User(or None)。

        無論 user 存不存在都跑一次 password hash,防止 timing 攻擊洩露
        username 是否存在(v1.1.7 之前的手刻 login 沒做這個防護)。
        """
        try:
            user = await self.get_by_username(username)
        except fa_exc.UserNotExists:
            self.password_helper.hash(password)
            return None
        verified, updated_hash = self.password_helper.verify_and_update(
            password, user.hashed_password
        )
        if not verified:
            return None
        # 漸進式 rehash:bcrypt → argon2 — 成功 verify 後若 helper 建議升級
        # 就持久化新 hash,下次登入直接走 argon2,平均一次登入完成一筆遷移。
        if updated_hash is not None:
            user.hashed_password = updated_hash
            await self.user_db.update(user, {"hashed_password": updated_hash})
        return user

    # ─ Lifecycle hooks ───────────────────────────────────────────────
    async def on_after_register(
        self, user: User, request: Optional[Request] = None
    ) -> None:
        if not user.must_change_password:
            user.must_change_password = True

    async def on_after_login(
        self, user: User, request: Optional[Request] = None, response=None
    ) -> None:
        from datetime import datetime
        user.last_login_at = datetime.utcnow()


async def get_user_manager(
    user_db: SQLAlchemyUserDatabase = Depends(get_user_db),
) -> AsyncGenerator[UserManager, None]:
    yield UserManager(user_db)


# ── JWT strategy ───────────────────────────────────────────────────────
# 標準 fastapi-users JWTStrategy 用 user.id (UUID) 當 sub。我們的
# SPA / Casbin / audit log 全部認 username — 換掉等於要動 SPA 100+ callsite。
# 這支改 sub = username,token shape 跟 v1.1.7 之前完全一樣,新舊 token 互通。

class UsernameSubJWTStrategy(JWTStrategy[User, str]):
    async def read_token(
        self, token: Optional[str], user_manager: BaseUserManager[User, str]
    ) -> Optional[User]:
        if token is None:
            return None
        # 不用 fastapi-users.jwt.decode_jwt — 它強制要 aud claim,但我們既有
        # token 沒有(對齊 v1.1.7 之前的格式)。直接用 PyJWT 並 audience=None
        # 跳過該檢查。
        try:
            data = jwt.decode(
                token,
                self.decode_key,
                algorithms=[self.algorithm],
                audience=None,
                options={"verify_aud": False},
            )
        except jwt.PyJWTError:
            return None
        sub = data.get("sub")
        if sub is None:
            return None
        # access token only,refresh 不能拿來認 user。
        if data.get("typ") != "access":
            return None
        try:
            assert isinstance(user_manager, UserManager)
            return await user_manager.get_by_username(sub)
        except fa_exc.UserNotExists:
            return None

    async def write_token(self, user: User) -> str:
        # 對齊 v1.1.7 之前 ``app.auth.security.create_access_token``:
        # sub=username, jti, iat, exp, typ="access", org_id, is_superuser
        import uuid as _uuid
        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone.utc)
        data = {
            "sub": user.username,
            "jti": _uuid.uuid4().hex,
            "iat": now,
            "exp": now + timedelta(seconds=self.lifetime_seconds),
            "typ": "access",
            "org_id": user.organization_id,
            "is_superuser": bool(user.is_superuser),
        }
        return jwt.encode(data, self.encode_key, algorithm=self.algorithm)


bearer_transport = BearerTransport(tokenUrl="/api/auth/login")


def get_jwt_strategy() -> UsernameSubJWTStrategy:
    """Factory for fastapi-users AuthenticationBackend AND auth/login router。

    Login endpoint 直接呼這支拿一個 strategy instance,然後
    ``await strategy.write_token(user)`` 換取 access token JWT。
    """
    return UsernameSubJWTStrategy(
        secret=JWT_SECRET,
        lifetime_seconds=ACCESS_TOKEN_TTL_MINUTES * 60,
        algorithm=JWT_ALGORITHM,
        token_audience=[],
    )


auth_backend = AuthenticationBackend(
    name="jwt",
    transport=bearer_transport,
    get_strategy=get_jwt_strategy,
)


fastapi_users = FastAPIUsers[User, str](get_user_manager, [auth_backend])

# fastapi-users 內建 active-user dependency 本身就會解 JWT + 撈 User。
# 我們再 wrap 一層加 must_change_password gate。
_fa_current_active_user = fastapi_users.current_user(active=True)
_fa_current_superuser = fastapi_users.current_user(active=True, superuser=True)


# ── must_change_password gate(自家 v1.1.6 概念,fastapi-users 沒有)──

_PASSWORD_RESET_ALLOWED_PATHS = (
    "/api/auth/me",
    "/api/auth/change-password",
    "/api/auth/profile-setup",
    "/api/auth/logout",
    "/api/auth/refresh",
)


def _path_is_password_reset_allowed(path: str) -> bool:
    return any(path.startswith(p) for p in _PASSWORD_RESET_ALLOWED_PATHS)


async def current_active_user(
    request: Request,
    user: User = Depends(_fa_current_active_user),
) -> User:
    """v1.1.8 取代 v1.1.7 之前的 ``app.auth.dependencies.get_current_user``。

    Lookup 走 fastapi-users 的 JWTStrategy + UserManager;``must_change_password``
    gate 由我們自家補回去。
    """
    if user.must_change_password and not _path_is_password_reset_allowed(
        request.url.path
    ):
        raise HTTPException(
            status_code=403,
            detail={
                "code": "must_change_password",
                "message": "首次登入請先修改密碼",
            },
        )
    return user


async def current_active_superuser(
    request: Request,
    user: User = Depends(_fa_current_superuser),
) -> User:
    if user.must_change_password and not _path_is_password_reset_allowed(
        request.url.path
    ):
        raise HTTPException(
            status_code=403,
            detail={
                "code": "must_change_password",
                "message": "首次登入請先修改密碼",
            },
        )
    return user


async def get_optional_user(
    request: Request,
    user_manager: UserManager = Depends(get_user_manager),
) -> Optional[User]:
    """匿名也 ok 的版本。讀 request.state.user_payload(由 middleware 預先解過)
    來判斷有沒有登入,有就用 username 撈 User、沒有回 None。"""
    payload = getattr(request.state, "user_payload", None)
    if not payload:
        return None
    username = payload.get("sub")
    if not username:
        return None
    try:
        user = await user_manager.get_by_username(username)
    except fa_exc.UserNotExists:
        return None
    if not user.is_active:
        return None
    return user


__all__ = [
    "UserManager",
    "UsernameSubJWTStrategy",
    "auth_backend",
    "fastapi_users",
    "current_active_user",
    "current_active_superuser",
    "get_optional_user",
    "get_user_db",
    "get_user_manager",
    "get_jwt_strategy",
]
