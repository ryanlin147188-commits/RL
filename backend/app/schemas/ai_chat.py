"""AI 對話 Pydantic schemas。"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict


class AiMessageResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    conversation_id: str
    role: str
    content: str
    tokens_used: Optional[int] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    created_at: datetime


class AiConversationCreate(BaseModel):
    title: Optional[str] = None
    provider_config_id: Optional[str] = None


class AiConversationUpdate(BaseModel):
    title: Optional[str] = None
    provider_config_id: Optional[str] = None


class AiConversationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    owner: str
    organization_id: Optional[str] = None
    title: str
    provider_config_id: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    # 顯示用:最後一則訊息預覽 + message 數
    message_count: int = 0
    last_message_preview: Optional[str] = None


class AiConversationDetail(AiConversationResponse):
    messages: list[AiMessageResponse] = []


class SendMessageRequest(BaseModel):
    content: str  # user 端發出的 message


class SendMessageResponse(BaseModel):
    user_message: AiMessageResponse
    assistant_message: AiMessageResponse
