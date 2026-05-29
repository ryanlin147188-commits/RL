"""AgentSession + AgentMessage ORM Models — Phase 1a。

對話流程:
    user 建 session → 送一條 message → service 寫 user message
    → 抓 session 內歷史 → 呼叫 LLMProvider.chat()
    → 寫 assistant message(可含 tool_calls,Phase 1b 才會有)
    → 同時寫一筆 AgentTokenUsage(風險紅線:成本可見度)

設計決定:
* session 是 **per-user**,組織內其他人看不到別人的對話。superuser 可看全 org
  (沿用 TenantQuery 的 superuser bypass 模式)。
* message.role 用 String(16) 而非 Enum,呼應 LLM Role 的 user/assistant/tool/system
  字串值,避免 schema migration 痛。
* ``tool_calls`` / ``tool_call_id`` Phase 1a 不會寫入,但 schema 先預留 — 避免
  Phase 1b 接 tool 時還要 alter table。
* ``token_usage_id`` 軟連結到 agent_token_usage:assistant message 對應的單筆
  usage 明細,方便前端「每條 assistant 訊息顯示成本」。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class AgentSession(Base):
    __tablename__ = "agent_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    organization_id: Mapped[Optional[str]] = mapped_column(
        String(36),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    # User 自填或從第一條訊息前 50 字自動取的標題。讓 user 在 session 列表能看到。
    title: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    # 該 session 使用的 model;空 = 用 settings.AGENT_DEFAULT_MODEL。
    # 允許 per-session override 而非 per-message,避免對話中途換 model 造成
    # context 不連續(不同家供應商的 system prompt cache 失效)。
    model: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    # Phase 2:session 模式。"chat"(預設、Phase 1)/ "planner"(從需求拆解測試案例)/
    # "analyzer"(吃 failed report 自動分析 root cause)。不同 mode 走不同 system
    # prompt 與 max_iterations,但共用同一個 tool registry。
    mode: Mapped[str] = mapped_column(
        String(16), nullable=False, default="chat", server_default="chat", index=True
    )
    # v1.2.x:是否啟用 mem0 跨 session 長期記憶。預設 True(per-session opt-out);
    # user 在浮動聊天框 toggle 就改這欄。False 時 send_message 不 recall 也不 add。
    memory_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    # 系統提示。Phase 1a 預設由 service 填一份;Phase 1b 起 tool 進來時會擴充。
    system_prompt: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Phase 2a:active skill。set 後 _compose_system_prompt 會把 skill 的
    # system_prompt_addition append 到 base mode prompt 尾端,且
    # compose_tools_for_session 會套用 skill.allowed_tools 白名單。
    # ondelete=SET NULL:skill 被刪掉時 session 自動退回「無 skill」,不丟資料。
    active_skill_id: Mapped[Optional[str]] = mapped_column(
        String(36),
        ForeignKey("skills.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        index=True,
    )


class AgentMessage(Base):
    __tablename__ = "agent_messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("agent_sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # "user" / "assistant" / "tool" / "system" — 對應 app.llm.base.Role
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    # User/Assistant 是純文字;Tool 是「tool 執行結果」字串(可為 JSON)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # Assistant 訊息要求工具呼叫時用;格式:[{"id": ..., "name": ..., "arguments": {...}}, ...]
    tool_calls: Mapped[Optional[list[dict[str, Any]]]] = mapped_column(JSON, nullable=True)
    # Role=tool 時必填,配對上一條 assistant 的 tool_call_id
    tool_call_id: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    # Phase 1c-1:非同步 tool 派出 Celery 後,task_id 存在這欄。
    # 前端拿這個 id 去 polling /api/executions/{task_id}/status,
    # 或 Phase 1c-2 起訂閱 WS /ws/v1/executions/{task_id}/logs。
    # 同步 tool 永遠是 None。
    task_id: Mapped[Optional[str]] = mapped_column(
        String(36), nullable=True, index=True
    )
    # Phase 1c-2:requires_confirmation 的 tool 派出後寫 pending tool message
    # 帶 placeholder JSON;這欄 FK 到 pending_actions.id。使用者 approve 後
    # 後端 update 同一條 message 的 content 為真結果。FK ondelete=SET NULL
    # 避免清 PendingAction 時把 message 連帶刪掉(歷史保留)。
    pending_action_id: Mapped[Optional[str]] = mapped_column(
        String(36),
        ForeignKey("pending_actions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # Assistant 訊息軟連結到 agent_token_usage 那筆;前端顯示「本訊息 $0.0021」
    token_usage_id: Mapped[Optional[str]] = mapped_column(
        String(36),
        ForeignKey("agent_token_usage.id", ondelete="SET NULL"),
        nullable=True,
    )
    # 訊息序號(in session);DB 用 created_at 排已足夠但加 seq 給 UI 更穩定
    seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow, index=True
    )
