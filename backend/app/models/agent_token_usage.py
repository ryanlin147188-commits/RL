"""AgentTokenUsage ORM Model — 每次 LLM chat 的 token 用量與成本明細。

每呼叫 LLM 一次寫一筆,用於:
1. **成本可見度**(風險紅線):前端聊天框 footer 顯示「本 session: $0.0421」
2. **月度 budget cap**:Phase 1+ 可加排程任務從這表 sum 月用量
3. **稽核**:出現異常用量時可回查是哪個 user / session / model
4. **計費**(若未來做 multi-tenant SaaS):accrual 都在這

設計決定:
* ``cost_usd`` 用 ``Numeric(10,6)`` 而非 Float — 金額不可有浮點誤差
* ``session_id`` 是 String 不是 FK — Phase 0 還沒 agent_sessions 表,先用
  字串記錄,Phase 1 建表後可選擇性 backfill / 補 FK
* 不存實際 prompt / response — 那是 agent_messages 的事,這裡只記 usage
* 不刪;歷史不可竄改(類似 audit_log)。要清舊資料走 retention policy
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Integer, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class AgentTokenUsage(Base):
    __tablename__ = "agent_token_usage"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)

    # 歸屬 — 拆分讓「組織月度成本」「使用者個人成本」「session 級成本」三種
    # 查詢都能直接走索引,不用 join。
    organization_id: Mapped[Optional[str]] = mapped_column(
        String(36),
        ForeignKey("organizations.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    user_id: Mapped[Optional[str]] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # Agent session id;Phase 0 還沒有 agent_sessions 表,先用字串。
    # Phase 1 建表後此欄會對應 agent_sessions.id;為了不要先加 FK 卡住建表順序,
    # 暫時不加 FK constraint,只加索引方便依 session 撈。
    session_id: Mapped[Optional[str]] = mapped_column(
        String(36), nullable=True, index=True
    )

    # 哪家、哪個 model
    provider: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    model: Mapped[str] = mapped_column(String(120), nullable=False)

    # Token 用量(來自 LLMProvider.Usage)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cache_write_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # 成本(USD)— 由 pricing.compute_cost_usd 算好寫入
    # Numeric(10, 6) 上限 9999.999999;單次呼叫絕不會超過
    cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(10, 6), nullable=False, default=Decimal("0")
    )

    # Anthropic-style normalized:end_turn / tool_use / max_tokens / stop_sequence
    stop_reason: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    # provider 回的 response id(Anthropic msg_xxx / OpenAI chatcmpl-xxx / Google responseId)
    response_id: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow, index=True
    )
