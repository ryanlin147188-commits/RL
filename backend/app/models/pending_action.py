"""PendingAction ORM Model — Phase 1c-2 二次確認紅線實作。

當 LLM 喚了 ``requires_confirmation=True`` 的 tool(如 ``run_test_case``)時,
dispatcher 不直接執行,而是:
1. 寫一筆 PendingAction(status=pending,arguments 完整序列化)
2. 寫一條 tool message 含 placeholder JSON("awaiting_confirmation")給 LLM 看,
   讓 LLM 回使用者「請按下確認按鈕」
3. 使用者按 approve → 後端用 PendingAction.arguments 重跑 dispatch(bypass
   confirm guard),更新原 tool message content 為真結果 + 跑 follow-up chat
4. 使用者按 reject → 設 status=rejected,更新 tool message 為「user_rejected」+
   跑 follow-up chat

設計決定:
* 不存 raw LLM message snapshot — 由 agent_messages 表保留即可
* ``arguments`` 用 JSON 欄而非 JSONB(SQLite 相容,雖然主用 PG;沿用其他表)
* ``status`` 用 String(16) 非 Enum — 三家 LLM 跟 ApiKey / ReviewStatus 等模式一致
* ``expires_at`` 預設 30 分鐘;過期 approve / reject 一律 422,前端應提示重新派
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Optional

from sqlalchemy import DateTime, ForeignKey, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


# Status 有限集合(用字串而非 Enum)
PENDING_STATUS_PENDING = "pending"
PENDING_STATUS_APPROVED = "approved"
PENDING_STATUS_REJECTED = "rejected"
PENDING_STATUS_EXPIRED = "expired"

ALL_PENDING_STATUSES = (
    PENDING_STATUS_PENDING,
    PENDING_STATUS_APPROVED,
    PENDING_STATUS_REJECTED,
    PENDING_STATUS_EXPIRED,
)


def _default_expiry() -> datetime:
    return datetime.utcnow() + timedelta(minutes=30)


class PendingAction(Base):
    __tablename__ = "pending_actions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("agent_sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # LLM 派 tool 的 tool_use id;approve 時要拿這個 id 寫對應的 tool message
    tool_call_id: Mapped[str] = mapped_column(String(120), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    # 完整的 tool arguments(JSON),approve 時用這份重跑 dispatch
    arguments: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)

    # pending / approved / rejected / expired
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=PENDING_STATUS_PENDING, index=True
    )
    # 給前端顯示「你即將執行 X,請確認」的人類可讀說明
    summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_default_expiry
    )
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
