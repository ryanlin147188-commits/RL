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


class BootstrapInviteRequest(BaseModel):
    """Bootstrap 第一張 admin 邀請碼。

    雙重閘門:
      * AUTOTEST_BOOTSTRAP_TOKEN env 必須設 (operator-controlled secret)
      * 目標 org 必須沒有任何 active admin (避免覆蓋既有部署)

    任一條件不滿足 → 端點拒絕。設計目的是讓「第一次部署的客戶」可以從
    UI 走完整個註冊流程,不需要 SSH 進 host 跑 CLI。
    """
    bootstrap_token: str
    organization_slug: str = "default"
    email: Optional[str] = None
    ttl_hours: int = 24


class RedeemInviteRequest(BaseModel):
    """Logged-in user pastes an invite code to (re)assign their org/role/group."""
    invite_token: str


class RedeemInviteResponse(BaseModel):
    """Returned after a successful redeem; the access_token is re-issued so
    the new organization_id is reflected in the JWT claim immediately."""
    organization_slug: str
    organization_name: str
    role_assigned: Optional[str] = None
    group_assigned: Optional[str] = None
    access_token: str
    refresh_token: str
    expires_in: int


class RequestAccessRequest(BaseModel):
    """Anonymous self-service invite request.

    The caller supplies an email; the server looks up an Organization that
    claims the email's @domain via Organization.email_domains, mints an
    invite, and emails the token to the requester. The token is NEVER
    returned in the HTTP response — only via email — so a third party who
    learns the email cannot pivot to the invite token without also reading
    the inbox."""
    email: str
    display_name: Optional[str] = None


class RequestAccessResponse(BaseModel):
    sent: bool = True
    organization_slug: str
    masked_email: str


class BootstrapInviteResponse(BaseModel):
    invite_token: str
    organization_id: str
    organization_slug: str
    role: str
    expires_at: datetime
    note: str = (
        "Use this token in POST /api/auth/register as `invite_token`. "
        "Single-use; expires at the time above."
    )
