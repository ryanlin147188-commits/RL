"""Auth Pydantic Schemas — 登入 / Token / 使用者資訊。"""
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int   # 單位：秒；前端用來決定何時 silent refresh


class RefreshRequest(BaseModel):
    refresh_token: str


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    username: str
    display_name: Optional[str] = None
    email: Optional[str] = None
    avatar_url: Optional[str] = None
    role_id: Optional[str] = None
    organization_id: Optional[str] = None
    is_active: bool = True
    is_superuser: bool = False
    last_login_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime


class UserCreateRequest(BaseModel):
    """建立新使用者(管理者用)。"""
    username: str
    password: str
    display_name: Optional[str] = None
    email: Optional[str] = None
    role_id: Optional[str] = None
    organization_id: Optional[str] = None
    is_superuser: bool = False


class UserUpdateMeRequest(BaseModel):
    """目前登入的使用者更新自己的個人資料(/auth/me PUT)。"""
    display_name: Optional[str] = None
    email: Optional[str] = None
    role_id: Optional[str] = None  # 想換角色


class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str


class RegisterRequest(BaseModel):
    """自助註冊。歸屬邏輯(優先順序由高到低):
    1. invite_token 存在 + 有效 → 用 invite 的 org / role / group
    2. email 後綴 match 某 org 的 email_domains → 自動加入
    3. 兩者都無 → 拒絕(避免任意人都能搶到 default org)
    """
    username: str
    password: str
    email: Optional[str] = None
    display_name: Optional[str] = None
    invite_token: Optional[str] = None
