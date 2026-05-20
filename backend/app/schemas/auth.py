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
    # 若為 True,前端登入後第一件事必須走 /auth/change-password,在那之前所有
    # 其他 API 都會被後端攔成 403。預設 False (一般登入流程)。
    must_change_password: bool = False


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
    must_change_password: bool = False
    last_login_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime
    # OIDC-only 帳號(zoho / casdoor 等)的 provider key;密碼登入帳號為 None。
    # 前端 #usersettings 用它判斷「變更密碼」卡片要不要顯示 — SSO 帳號沒真實密碼,
    # 改了也沒意義(password_hash 只是隨機 32-byte token)。
    oidc_provider: Optional[str] = None
    # /api/auth/me 會把 role.permissions_json 解出來填進來,給前端 capability gate;
    # superuser 一律拿到 ["*"] (萬用權限,前端 hasPerm 直接 short-circuit)。
    # 其他 endpoint 回 UserResponse 時若沒填則保持 [],前端 hasPerm fail-safe deny。
    permissions: list[str] = []


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


class UserAdminUpdateRequest(BaseModel):
    """Superuser 用 PUT /auth/users/{u} 改別人。

    密碼/帳號名/organization_id 不在這裡改:密碼走 reset-password,
    organization_id 走 switch-org / 重新建立。所有欄位都是 optional,
    缺省 = 不動。
    """
    display_name: Optional[str] = None
    email: Optional[str] = None
    role_id: Optional[str] = None
    is_active: Optional[bool] = None
    is_superuser: Optional[bool] = None


class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str


class UserResetPasswordRequest(BaseModel):
    """Superuser 替別人 reset 密碼。對方下次登入會被強制改密碼。"""
    new_password: str


class ForgotPasswordRequest(BaseModel):
    """忘記密碼:輸入 username + email,系統寄重置連結到 email。"""
    username: str
    email: str


class ForgotPasswordResponse(BaseModel):
    """為避免洩露帳號是否存在,後端永遠回 ``{"sent": true}`` + 通用訊息。"""
    sent: bool = True
    message: str = "若帳號 / Email 正確,我們已寄出重置連結,請至信箱查收"


class ResetPasswordRequest(BaseModel):
    """從 email 連結點進來後,提交 token + 新密碼。"""
    token: str
    new_password: str


class ResetPasswordTokenInfo(BaseModel):
    """前端載入頁面後預先驗證 token 用,僅回傳是否有效跟過期時間。
    不洩露 username,避免 token 流出後別人能拼湊出帳號名。"""
    valid: bool
    expires_at: Optional[datetime] = None


