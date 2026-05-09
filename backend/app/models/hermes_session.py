"""Hermes session metadata pointer table。

PR3:取代舊 ai_conversations / ai_messages 兩張表的最小 metadata 層。

設計取捨:
- 訊息內容(history、tool calls、memory)由 Hermes sidecar 的 SQLite + FTS5 全權管理。
  Backend 不雙寫訊息,避免 Hermes 升級後 schema 漂移。
- 但前端要列「過去對話清單」與標題,Hermes 沒提供標題,且 ACP `session/list` 在
  訊息持久化前可能還沒 visible。所以這裡只存最薄的 metadata 指標:
  session_id(來自 Hermes)、owner、title、last_message_preview、updated_at。
- 重新整理進舊 session 時訊息列會空 — v1 接受;之後若要還原訊息,再加 message
  快取或讓 Hermes 暴露 history endpoint。
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class HermesSessionRef(Base):
    __tablename__ = "hermes_session_refs"

    # 主鍵直接用 Hermes 回的 session_id(UUID 36),不再多生一個 surrogate id —
    # 任何時候 backend ↔ sidecar 傳的就是這個值。
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # workspace_id = ws_<user.id>;sidecar 強制檢查不接受 client 自帶,
    # 由 backend 在 router 層用 user.id 計算後傳。
    workspace_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    organization_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    # owner=username 沿用 ai_conversations 慣例(與 notification 等同 pattern)
    owner: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False, default="新對話")
    # 列表頁顯示用;每次 send 後更新
    last_message_preview: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )
