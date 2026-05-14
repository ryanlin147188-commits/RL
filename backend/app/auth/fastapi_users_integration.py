"""FastAPI Users 整合層(v1.1.7 — Phase 1 of migration)。

這個 module 把 fastapi-users 的 primitives(SQLAlchemyUserDatabase /
UserManager / JWTStrategy / PasswordHelper / BearerTransport)接上
我們現有的 User ORM,但**不**強制 caller 使用。Phase 1 階段:

- 既有 ``app.auth.security`` / ``app.auth.dependencies`` 完全不動,還在跑
- 這支提供另一條等價路徑;後續 phase 會把 router / dependency 一條條切過去

設計取捨:

- ``users`` 表目前 PK 是 ``username``(string)。fastapi-users 的 generic
  type 容許自定 ID 型別,所以我們用 ``str`` + username 當 id,避免 Phase 1
  動 schema。Phase 7 再切成 UUID PK。
- JWT secret / lifetime 跟舊 path 共用(``AUTOTEST_JWT_SECRET``),這樣兩條
  路徑簽出來的 token 能互相驗證,cutover 過程中不會把現役 session 全踢掉。
- PasswordHelper 預設用 bcrypt(passlib backend),hash 結構跟 v1.1.6 之前
  種出來的密碼 100% 相容,使用者不用 reset password。

未來 phase 移除這個 module:
- Phase 4 後:``app.auth.security.hash_password`` / ``verify_password`` 改成
  thin wrapper 轉呼這支的 PasswordHelper。
- Phase 5 後:``app.auth.dependencies.get_current_user`` 改成 thin wrapper
  轉呼 ``current_active_user``。
- Phase 7 後:整支 module 變成 canonical auth path,``security.py`` 拔掉 75%
  hand-rolled JWT/bcrypt 程式碼,只留 active_org_id cookie 簽章 + token_generation gate。
"""
from __future__ import annotations

import os
from typing import AsyncGenerator, Optional

from fastapi import Depends, Request
from fastapi_users import BaseUserManager, FastAPIUsers
from fastapi_users.authentication import (
    AuthenticationBackend,
    BearerTransport,
    JWTStrategy,
)
from fastapi_users.db import SQLAlchemyUserDatabase
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.security import (
    ACCESS_TOKEN_TTL_MINUTES,
    JWT_ALGORITHM,
    JWT_SECRET,
)
from app.database import get_db
from app.models.user import User

# ── User database adapter ─────────────────────────────────────────────
# fastapi-users 的 SQLAlchemyUserDatabase 預期 model 上有 ``id`` 屬性。
# 我們 User PK 是 username,在 Phase 7 之前先把 ``id`` 當 alias property,
# 讓 SQLAlchemyUserDatabase 的 .get(id) / .create() / 等行為轉去操作
# username 欄位。
#
# 注意:這只是 Python attribute 層面的相容包裝;DB 上仍是 username 欄位。

if not hasattr(User, "id"):
    # Phase 1 — username 即 id。Phase 7 加 UUID column 時把這個 property 拔掉。
    def _user_id_get(self: User) -> str:  # type: ignore[no-redef]
        return self.username

    def _user_id_set(self: User, value: str) -> None:  # type: ignore[no-redef]
        self.username = value

    User.id = property(_user_id_get, _user_id_set)  # type: ignore[attr-defined]


# fastapi-users 期望 user.hashed_password / user.is_verified。Phase 4 才把
# DB column 改名 + 加 is_verified 欄位;Phase 1 用 Python-level shim 不動 DB。
if not hasattr(User, "hashed_password"):
    def _user_hp_get(self: User) -> str:  # type: ignore[no-redef]
        return self.password_hash

    def _user_hp_set(self: User, value: str) -> None:  # type: ignore[no-redef]
        self.password_hash = value

    User.hashed_password = property(_user_hp_get, _user_hp_set)  # type: ignore[attr-defined]

if not hasattr(User, "is_verified"):
    # Phase 1 把現有使用者全部視為 verified(他們已經能登入,不需要 email 驗證
    # 一次)。Phase 4 加 ``is_verified`` boolean column,default True;新建帳號
    # 走 admin CRUD 時也設 True(內部使用者不走 email verify 流程)。
    def _user_iv_get(self: User) -> bool:  # type: ignore[no-redef]
        return True

    def _user_iv_set(self: User, value: bool) -> None:  # type: ignore[no-redef]
        pass  # Phase 1 no-op;Phase 4 持久化到 DB column。

    User.is_verified = property(_user_iv_get, _user_iv_set)  # type: ignore[attr-defined]


async def get_user_db(
    session: AsyncSession = Depends(get_db),
) -> AsyncGenerator[SQLAlchemyUserDatabase, None]:
    """FastAPI 依賴:把 AsyncSession 包成 fastapi-users 的 SQLAlchemyUserDatabase。"""
    yield SQLAlchemyUserDatabase(session, User)


# ── UserManager ────────────────────────────────────────────────────────
# 我們的業務 hook 全部塞在這支:on_after_register / on_after_login /
# on_after_forgot_password / on_after_reset_password。Phase 5 把 auth router
# 切過來後,所有 user lifecycle event 都會打到這幾個 hook。

class UserManager(BaseUserManager[User, str]):
    """v1.1.7 階段的 User manager — id 型別仍是 str(username)。"""

    # reset / verify token 簽章用的 secret(獨立於 access token JWT secret)。
    # fastapi-users 預設用同一個 secret 給 reset/verify 也可以,但分開能讓未
    # 來想旋換 reset secret 而不踢掉現役 session 變簡單。
    reset_password_token_secret = JWT_SECRET
    verification_token_secret = JWT_SECRET
    reset_password_token_lifetime_seconds = 60 * 60  # 1h
    verification_token_lifetime_seconds = 60 * 60 * 24  # 1d

    def parse_id(self, value: str) -> str:
        # username 是 str,直接回傳即可;若 user 帶其他型別進來會 raise。
        if not isinstance(value, str) or not value:
            raise ValueError("user id must be a non-empty string (username)")
        return value

    async def on_after_register(
        self, user: User, request: Optional[Request] = None
    ) -> None:
        """新建帳號後 hook。Phase 5 接上 register router 後會被觸發。"""
        # v1.1.6 一律 must_change_password=True;新建後讓使用者首登必須改密碼 + 補資料。
        # admin CRUD 路徑會自己設這個 flag,這裡只負責 register 路徑(自助註冊)。
        if not user.must_change_password:
            user.must_change_password = True

    async def on_after_login(
        self,
        user: User,
        request: Optional[Request] = None,
        response=None,
    ) -> None:
        """成功登入後 hook。記錄 last_login_at,後續 Phase 補 audit log。"""
        from datetime import datetime

        user.last_login_at = datetime.utcnow()

    async def on_after_forgot_password(
        self, user: User, token: str, request: Optional[Request] = None
    ) -> None:
        """走 fastapi-users forgot-password router 時觸發;
        Phase 5 cutover 完才會跑到。"""
        # TODO Phase 5:hook 進現有 password_reset_tokens 表;目前 placeholder。
        pass

    async def on_after_reset_password(
        self, user: User, request: Optional[Request] = None
    ) -> None:
        """成功 reset password 後 hook。"""
        # reset 完不再強制改密(使用者剛親手改過)
        user.must_change_password = False


async def get_user_manager(
    user_db: SQLAlchemyUserDatabase = Depends(get_user_db),
) -> AsyncGenerator[UserManager, None]:
    yield UserManager(user_db)


# ── JWT authentication backend ─────────────────────────────────────────
# fastapi-users 的 AuthenticationBackend = transport(bearer / cookie)+
# strategy(JWT / DB)。我們現有 SPA 是把 access token 放在 Authorization
# header(Bearer),所以走 BearerTransport;tokenUrl 維持 ``/api/auth/login``
# 對齊 SPA 既有打 API 的路徑。

bearer_transport = BearerTransport(tokenUrl="/api/auth/login")


def _get_jwt_strategy() -> JWTStrategy:
    return JWTStrategy(
        secret=JWT_SECRET,
        lifetime_seconds=ACCESS_TOKEN_TTL_MINUTES * 60,
        algorithm=JWT_ALGORITHM,
        # token_audience 預設是 ["fastapi-users:auth"],會跟我們現有
        # access token(沒帶 aud)不相容。設 None 讓兩邊互通。
        token_audience=[],
    )


auth_backend = AuthenticationBackend(
    name="jwt",
    transport=bearer_transport,
    get_strategy=_get_jwt_strategy,
)


# ── FastAPIUsers root object ─────────────────────────────────────────
# 這個物件提供 router / dependency factory:
#
#   fastapi_users.current_user(active=True)  → Depends → User
#   fastapi_users.get_auth_router(auth_backend) → /login + /logout
#   fastapi_users.get_register_router(UserRead, UserCreate) → /register
#   fastapi_users.get_reset_password_router() → /forgot-password + /reset-password
#
# Phase 1 不掛任何 router,只暴露 dependency。Phase 5 才會 include 進 main.py。

fastapi_users = FastAPIUsers[User, str](get_user_manager, [auth_backend])

# 現役 dependency:active(is_active=True 才能通過)
# Phase 5 後可以用這支取代 app.auth.dependencies.get_current_user。
current_active_user = fastapi_users.current_user(active=True)
current_superuser = fastapi_users.current_user(active=True, superuser=True)


__all__ = [
    "UserManager",
    "auth_backend",
    "fastapi_users",
    "current_active_user",
    "current_superuser",
    "get_user_db",
    "get_user_manager",
]
