"""Test Data Set (DDT) ORM Model。

獨立的「測試資料集」實體，將 DDT (Data-Driven Testing) 從測試案例中抽出，
讓多個測試案例可以共用同一份資料表（例：登入帳密、商品資料、邊界值列表）。

每個資料集是一個簡單的「行 × 欄」表格：
- columns_json: ["account", "password", "expected_role", ...]
- rows_json: [{"account": "alice", "password": "...", "expected_role": "admin"}, ...]

linked_testcase_ids 是備註用途的字串列表（測試案例 ID），實際綁定關係由前端在
案例編輯時引用 dataset.code 來達成；這裡保留欄位是為了未來做反向追溯。
"""
from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import JSON, DateTime, Enum, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.auth.tenant import TenantScoped
from .base import Base


class DataSetCategory(str, enum.Enum):
    LOGIN = "Login"
    BOUNDARY = "Boundary"
    BUSINESS = "Business"
    PERFORMANCE = "Performance"
    OTHER = "Other"


class TestDataSet(TenantScoped, Base):
    __tablename__ = "test_data_sets"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    code: Mapped[str] = mapped_column(String(40), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    category: Mapped[DataSetCategory] = mapped_column(
        Enum(DataSetCategory), default=DataSetCategory.OTHER, nullable=False
    )
    columns_json: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    rows_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    linked_testcase_ids: Mapped[Optional[list[str]]] = mapped_column(JSON, nullable=True, default=list)
    owner: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )
