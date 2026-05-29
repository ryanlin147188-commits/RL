"""Skill ORM Model — Phase 2a:per-org 工作流模板

Skill = 「啟用後 append 一段 system prompt + 限縮 LLM 能用的 tool 集合」。
不是 tool 本身,是「對話模式 / playbook」,讓使用者一鍵切到 BDD 寫測試、
分析失敗 root cause 等工作脈絡。

設計重點:
* per-org:每個組織各自管理清單,不共用。
* allowed_tools 是 **glob 白名單**(None / 空 = 不限縮);支援
  ``mcp__playwright__*`` 這種通配,讓 Skill 也能對未來的 MCP tool 套用。
* system_prompt_addition 不取代 base mode prompt,而是在組 prompt 時 append
  一個 ``## Active Skill: <name>`` section。切換 / 取消 skill 立即生效,
  不污染 session.system_prompt 那欄(保留給 stale check 用)。
* trigger_keywords 給 Phase 2b 的「auto-suggest chip」用,不是 auto-load。
  避免 prompt 被偷偷換掉造成 debug 困難。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class Skill(Base):
    __tablename__ = "skills"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # per-org unique;前端切換 skill 也用這個 name 顯示
    name: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # ["BDD","測試案例"] — 訊息含這些關鍵字時 backend 在 response metadata
    # 回 suggested_skill_id,前端跳 chip 讓 user 一鍵啟用(不自動套)。
    trigger_keywords: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list
    )
    # 啟用時 append 到 system prompt 的內容(markdown body)
    system_prompt_addition: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # tool 名稱 glob 白名單;None / 空 = 不限縮全部 tool 都能用
    # 例:["query_*", "create_testcase", "mcp__playwright__*"]
    allowed_tools: Mapped[Optional[list[str]]] = mapped_column(JSON, nullable=True)
    # 可用於哪些 session.mode;空 list = 全部 mode 都可用
    mode_scope: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    # 編輯一次 +1;前端 cache invalidation 用
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    created_by: Mapped[Optional[str]] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )
