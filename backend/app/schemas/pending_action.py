"""PendingAction Pydantic schemas — Phase 1c-2 二次確認 API。

approve / reject endpoint 沒有 request body(動作本身語意明確);response 統一回
``SendMessageResponse`` 風格(含 follow-up assistant message),讓前端 approve
完直接渲染新訊息,不必再多打一個 GET。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict


class PendingActionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    session_id: str
    user_id: str
    tool_call_id: str
    tool_name: str
    arguments: Optional[dict[str, Any]]
    status: str  # pending / approved / rejected / expired
    summary: Optional[str]
    created_at: datetime
    expires_at: datetime
    resolved_at: Optional[datetime]


class PendingActionResolveResponse(BaseModel):
    """approve / reject 完成後一次回:更新後的 pending 狀態 + tool message + follow-up assistant。

    前端直接拿這個 response 渲染聊天面板:tool message content 換成真結果(或
    user_rejected),底下接著新的 assistant message。"""

    pending: PendingActionResponse
    # 更新後的 tool message(role=tool, content 換成真結果或 rejected);
    # AgentMessageResponse 不直接 import 避免 circular,在 router 端組裝
    tool_message: dict[str, Any]
    # follow-up LLM 回應(可能還有 tool_use → 後續又有迴圈)
    assistant_message: dict[str, Any]
