"""Agent session / message Pydantic schemas — Phase 1a。

不含 tool_calls 結構驗證(Phase 1b 才會用);先用 ``Any`` 對 JSON 列存。
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


# ── Session ──────────────────────────────────────────────────────────


class AgentSessionCreate(BaseModel):
    """建立 session 時可選給 title / model / system_prompt;都不給時走 service 預設。"""

    title: Optional[str] = Field(default=None, max_length=120)
    # 不給 = 用 settings.AGENT_DEFAULT_MODEL
    model: Optional[str] = Field(default=None, max_length=120)
    # 不給 = service 用一份基礎系統提示("你是 RL 自動化測試平台的助手...")
    system_prompt: Optional[str] = Field(default=None, max_length=8000)


class AgentSessionUpdate(BaseModel):
    """目前支援改 title 與 memory_enabled。model / system_prompt 不改
    (對話中換 model 會破壞 cache;system_prompt 由 mode 與 mem0 recall 動態決定)。"""

    title: Optional[str] = Field(default=None, max_length=120)
    memory_enabled: Optional[bool] = Field(default=None, description="True/False;None = 不動既有值")


class AgentSessionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    user_id: str
    organization_id: Optional[str]
    title: Optional[str]
    model: Optional[str]
    system_prompt: Optional[str]
    memory_enabled: bool = True
    active_skill_id: Optional[str] = None
    created_at: datetime
    updated_at: datetime


# ── Message ──────────────────────────────────────────────────────────


class SendMessageRequest(BaseModel):
    """使用者送一條訊息。"""

    content: str = Field(min_length=1, max_length=32000)


class TokenUsageInfo(BaseModel):
    """assistant message 附帶的 token / cost 摘要,給前端顯示。"""

    model_config = ConfigDict(from_attributes=True)

    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    cost_usd: Decimal


class AgentMessageResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    session_id: str
    role: str
    content: str
    tool_calls: Optional[list[dict[str, Any]]] = None
    tool_call_id: Optional[str] = None
    # Phase 1c-1:非同步 tool 派 Celery 後的 task_id(同步 tool / 非 tool msg 為 None)
    # 前端用這個 id 去 polling /api/executions/{task_id}/status 或 WS 訂閱
    task_id: Optional[str] = None
    # Phase 1c-2:requires_confirmation tool 的 placeholder tool message 帶
    # PendingAction id,前端據此呼叫 /confirm endpoint。approve 後 update 為 None。
    pending_action_id: Optional[str] = None
    seq: int
    created_at: datetime
    # assistant 訊息會附 usage;user / tool 訊息為 None
    usage: Optional[TokenUsageInfo] = None


class SendMessageResponse(BaseModel):
    """送出後同時回傳剛存的 user message + LLM 回應的 assistant message。"""

    user_message: AgentMessageResponse
    assistant_message: AgentMessageResponse


# ── Phase 2:路線 B 自主 Agent ────────────────────────────────────


class PlannerRunRequest(BaseModel):
    """從需求文字啟動一個 planner session。"""

    requirement_text: str = Field(min_length=10, max_length=20000)
    project_id: Optional[str] = Field(default=None, description="若指定,planner 內的 tool 預設帶這個 project")
    model: Optional[str] = Field(default=None, max_length=120)


class AnalyzerRunRequest(BaseModel):
    """從 failed execution_report 啟動一個 analyzer session。"""

    report_id: str = Field(min_length=1, max_length=36)
    model: Optional[str] = Field(default=None, max_length=120)


class AutonomousRunResponse(BaseModel):
    """planner / analyzer endpoint 共用 response:回新 session + 第一輪 assistant 回應。"""

    session: AgentSessionResponse
    initial_user_message: AgentMessageResponse
    assistant_message: AgentMessageResponse
