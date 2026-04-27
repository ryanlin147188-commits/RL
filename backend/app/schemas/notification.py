"""Notification Pydantic Schemas — 站內通知 API 輸出輸入結構。"""
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class NotificationCreate(BaseModel):
    recipient: str
    title: str = Field(..., max_length=300)
    body: Optional[str] = None
    level: str = Field("info", pattern="^(info|success|warning|error)$")
    event_key: Optional[str] = None
    link: Optional[str] = None
    related_entity_type: Optional[str] = None
    related_entity_id: Optional[str] = None


class NotificationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    recipient: str
    title: str
    body: Optional[str] = None
    level: str
    event_key: Optional[str] = None
    link: Optional[str] = None
    related_entity_type: Optional[str] = None
    related_entity_id: Optional[str] = None
    is_read: bool
    read_at: Optional[datetime] = None
    created_at: datetime


class UnreadCountResponse(BaseModel):
    count: int
