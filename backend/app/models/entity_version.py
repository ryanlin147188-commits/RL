"""EntityVersion — 通用 entity 版本快照表(支援 AI/human/system/revert 來源 + 任意還原)。

每次 entity(testcase / defect / requirement / test_document / wbs_item / todo)
建立或修改,都會 mirror 一筆完整內容快照到此表。配合主表的 ``content_status``
欄位(``ai_draft`` / ``pending_review`` / ``approved`` / ``rejected``)構成
「AI 生成 → 審核前 → 審核後」的完整生命週期 + 任意回滾。

設計重點:
  * **polymorphic**:用 ``entity_type`` + ``entity_id`` pair 對應到主表 row;
    避免每個 entity 各開一張歷史表。
  * **content_snapshot 是 JSON**:把 entity 當下所有業務欄位序列化進去,
    revert 時直接從這個 JSON 反序列化覆蓋回主表。
  * **append-only**:沒有 update / delete 自己的 row,只有新增。
  * **version_no per (entity_type, entity_id) 序列**:讓 UI 可以顯示
    「v1 / v2 / v3」這種人類可讀的版本號。
  * **parent_version_id**:revert 後的新 row 會把 parent 設為來源的 version_id,
    形成可視化的時間樹。
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


# ─── content_status 枚舉值(主表 + 快照都用這四個值)─────────────────
CONTENT_STATUS_AI_DRAFT = "ai_draft"           # AI 生成,未送審
CONTENT_STATUS_PENDING = "pending_review"      # 已送審,等管理員過
CONTENT_STATUS_APPROVED = "approved"           # 審核通過,正式上線版
CONTENT_STATUS_REJECTED = "rejected"           # 審核被駁回

# ─── change_source 枚舉值 ──────────────────────────────────────────
CHANGE_SOURCE_HUMAN = "human"      # 一般使用者編輯
CHANGE_SOURCE_AI = "ai"            # AI 生成寫入
CHANGE_SOURCE_SYSTEM = "system"    # 系統自動(例如 review 通過時 flip 狀態)
CHANGE_SOURCE_REVERT = "revert"    # 由還原操作建立


class EntityVersion(Base):
    __tablename__ = "entity_versions"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4()),
    )
    entity_type: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    entity_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    # 同 (entity_type, entity_id) 內的單調遞增整數,從 1 開始;前端顯示 v1/v2/...
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    # 完整業務欄位的 JSON 序列化。revert 時拿這份覆蓋主表。
    content_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    # 此快照當下的狀態。隨著生命週期推進,新版的 content_status 會逐一覆蓋。
    content_status: Mapped[str] = mapped_column(String(20), nullable=False)
    change_source: Mapped[str] = mapped_column(String(20), nullable=False)
    changed_by: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    change_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # revert 時指向被還原的來源 version_id,形成「v3 (revert from v1)」這種時間樹
    parent_version_id: Mapped[Optional[str]] = mapped_column(
        String(36),
        ForeignKey("entity_versions.id", ondelete="SET NULL"),
        nullable=True,
    )
    organization_id: Mapped[Optional[str]] = mapped_column(
        String(36),
        ForeignKey("organizations.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False,
    )
