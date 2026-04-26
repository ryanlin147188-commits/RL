"""AuditLog 審計紀錄 ORM Model。

每一筆 mutating（POST/PUT/PATCH/DELETE）的 /api/* 請求會被中介層自動寫入一筆，
給合規（SOC2/ISO27001）與「誰改了什麼」追溯使用。
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import JSON, DateTime, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    # 後續會加 organization_id（多租戶）— 預留欄位
    organization_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)
    username: Mapped[Optional[str]] = mapped_column(String(100), nullable=True, index=True)
    method: Mapped[str] = mapped_column(String(10), nullable=False)
    path: Mapped[str] = mapped_column(String(500), nullable=False)
    # 從 path 推測的 entity（例：/api/defects/xxx → "defect"），方便依資源類型篩選
    entity_type: Mapped[Optional[str]] = mapped_column(String(60), nullable=True, index=True)
    # 從 path 抓出的最後一個 uuid 段；新建 (POST) 時是 None，update/delete 時有值
    entity_id: Mapped[Optional[str]] = mapped_column(String(80), nullable=True, index=True)
    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ip_address: Mapped[Optional[str]] = mapped_column(String(45), nullable=True)
    user_agent: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    request_query: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # 變更摘要（JSON）— 中介層暫不解析 body；保留欄位給日後高階審計（diff before/after）
    change_summary: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    error_detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


# 複合索引：常用查詢「最近某使用者的所有動作」、「特定資源的歷史」
Index("ix_audit_user_time", AuditLog.username, AuditLog.created_at.desc())
Index("ix_audit_entity_time", AuditLog.entity_type, AuditLog.entity_id, AuditLog.created_at.desc())
