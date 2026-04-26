"""Settings + Todo Pydantic Schemas（Role / NotificationPreference / EmailConfig / AiTokenConfig / TodoItem）。"""
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict


# ── Role ──────────────────────────────────────────────────────────────

class RoleBase(BaseModel):
    name: str
    description: Optional[str] = None
    permissions_json: list[str] = []


class RoleCreate(RoleBase):
    pass


class RoleUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    permissions_json: Optional[list[str]] = None


class RoleResponse(RoleBase):
    model_config = ConfigDict(from_attributes=True)
    id: str
    is_system: bool = False
    created_at: datetime
    updated_at: datetime


# ── NotificationPreference ────────────────────────────────────────────

class NotificationPreferenceBase(BaseModel):
    username: Optional[str] = None
    events_json: dict[str, Any] = {}


class NotificationPreferenceCreate(NotificationPreferenceBase):
    pass


class NotificationPreferenceUpdate(BaseModel):
    events_json: Optional[dict[str, Any]] = None


class NotificationPreferenceResponse(NotificationPreferenceBase):
    model_config = ConfigDict(from_attributes=True)
    id: str
    created_at: datetime
    updated_at: datetime


# ── EmailConfig ───────────────────────────────────────────────────────

class EmailConfigBase(BaseModel):
    smtp_host: Optional[str] = None
    smtp_port: Optional[int] = 587
    smtp_user: Optional[str] = None
    smtp_password: Optional[str] = None
    use_tls: bool = True
    from_address: Optional[str] = None
    from_name: Optional[str] = "AutoTest"
    enabled: bool = False


class EmailConfigUpdate(EmailConfigBase):
    pass


class EmailConfigResponse(EmailConfigBase):
    model_config = ConfigDict(from_attributes=True)
    id: str
    updated_at: datetime


# ── AiTokenConfig ─────────────────────────────────────────────────────

class AiTokenConfigBase(BaseModel):
    name: str
    provider: str = "OpenAI"
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    model: Optional[str] = None
    enabled: bool = True
    is_default: bool = False
    description: Optional[str] = None


class AiTokenConfigCreate(AiTokenConfigBase):
    pass


class AiTokenConfigUpdate(BaseModel):
    name: Optional[str] = None
    provider: Optional[str] = None
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    model: Optional[str] = None
    enabled: Optional[bool] = None
    is_default: Optional[bool] = None
    description: Optional[str] = None


class AiTokenConfigResponse(AiTokenConfigBase):
    model_config = ConfigDict(from_attributes=True)
    id: str
    created_at: datetime
    updated_at: datetime


# ── TodoItem ──────────────────────────────────────────────────────────

class TodoItemBase(BaseModel):
    project_id: Optional[str] = None
    title: str
    description: Optional[str] = None
    due_date: Optional[str] = None
    status: str = "Todo"
    priority: str = "P2"
    assignee: Optional[str] = None
    related_entity_type: Optional[str] = None
    related_entity_id: Optional[str] = None


class TodoItemCreate(TodoItemBase):
    pass


class TodoItemUpdate(BaseModel):
    project_id: Optional[str] = None
    title: Optional[str] = None
    description: Optional[str] = None
    due_date: Optional[str] = None
    status: Optional[str] = None
    priority: Optional[str] = None
    assignee: Optional[str] = None
    related_entity_type: Optional[str] = None
    related_entity_id: Optional[str] = None


class TodoItemResponse(TodoItemBase):
    model_config = ConfigDict(from_attributes=True)
    id: str
    completed_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime
    # 額外運算欄位：是否過期、距離到期天數
    is_overdue: bool = False
    days_to_due: Optional[int] = None
