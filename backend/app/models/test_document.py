"""Test Document ORM Model — 測試文件（Markdown 內容，支援 Mermaid 流程圖）。

每個專案下可以掛多份測試文件：例如『測試策略』、『環境準備手冊』、
『驗收條件總表』、『缺陷分流流程圖』、『效能基準』等。內容以 Markdown
儲存，前端使用 marked.js + mermaid.js 即時渲染預覽。
"""
from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Enum, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.auth.tenant import Assignable, TenantScoped
from .base import Base


class DocumentCategory(str, enum.Enum):
    STRATEGY = "Strategy"          # 測試策略 / 計畫補充
    GUIDE = "Guide"                # 操作手冊 / 環境準備
    RUNBOOK = "Runbook"            # 流程 / 緊急處理
    CHECKLIST = "Checklist"        # 檢查清單
    NOTE = "Note"                  # 一般筆記
    OTHER = "Other"


class TestDocument(Assignable, TenantScoped, Base):
    __tablename__ = "test_documents"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    code: Mapped[str] = mapped_column(String(40), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    category: Mapped[DocumentCategory] = mapped_column(
        Enum(DocumentCategory), default=DocumentCategory.NOTE, nullable=False
    )
    content_md: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    summary: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    owner: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    tags: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )
