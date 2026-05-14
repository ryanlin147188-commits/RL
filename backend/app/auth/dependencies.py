"""FastAPI dependencies — v1.1.8 cutover.

v1.1.8 起,``get_current_user`` / ``get_optional_user`` 變成
:mod:`app.auth.fastapi_users_integration` 內 fastapi-users 依賴的 thin alias。
260+ 個既有 ``Depends(get_current_user)`` callsite 從這個 commit 開始一律走
fastapi-users 的 JWTStrategy + UserManager + SQLAlchemyUserDatabase。

新寫 router 時請直接 import ``current_active_user`` / ``current_active_superuser``;
``get_current_user`` 保留是為了讓 600+ 行的 router code 不必一次全改、降低
regression 風險。

實際邏輯(must_change_password gate / 取 User ORM / 認 username sub)都在
:mod:`app.auth.fastapi_users_integration` — 這支只負責 re-export。
"""
from __future__ import annotations

from app.auth.fastapi_users_integration import (
    current_active_superuser,
    current_active_user,
    get_optional_user,
)

# v1.1.7 之前的名字。新 code 走 ``current_active_user``。
get_current_user = current_active_user

__all__ = [
    "current_active_user",
    "current_active_superuser",
    "get_current_user",
    "get_optional_user",
]
