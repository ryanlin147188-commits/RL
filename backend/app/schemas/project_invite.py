"""Pydantic schemas for project invite endpoints."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class ProjectInviteCreate(BaseModel):
    """admin 送邀請的請求 body。"""

    invitee_email: EmailStr
    role_id: Optional[str] = None  # 該專案內角色 override;預設沿用 org 角色
    expires_days: int = Field(default=7, ge=1, le=90)


class ProjectInviteResponse(BaseModel):
    """邀請物件(列表 / create 都用這個)。"""

    model_config = ConfigDict(from_attributes=True)
    id: str
    project_id: str
    invitee_email: str
    role_id: Optional[str] = None
    invite_code: str  # 列給 admin 看作 fallback (寄信失敗時可手動轉貼)
    inviter_username: str
    status: str
    created_at: datetime
    expires_at: datetime
    redeemed_at: Optional[datetime] = None
    redeemed_by_username: Optional[str] = None


class ProjectInviteRedeem(BaseModel):
    """使用者兌換邀請的請求 body。"""

    invite_code: str
