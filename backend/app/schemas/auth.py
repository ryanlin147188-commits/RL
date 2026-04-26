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
    role_id: Optional[str] = None
    organization_id: Optional[str] = None
    is_active: bool = True
    is_superuser: bool = False
    last_login_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime


class UserCreateRequest(BaseModel):
    """建立新使用者（管理者用）。"""
    username: str
    password: str
    display_name: Optional[str] = None
    email: Optional[str] = None
    role_id: Optional[str] = None
    organization_id: Optional[str] = None
    is_superuser: bool = False


class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str
