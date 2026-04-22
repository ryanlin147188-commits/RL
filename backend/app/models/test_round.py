"""測試回合（Test Round）ORM Model。

一個測試回合 = 一組被命名起來、要一起執行的測試案例集合。
使用情境：
  - 冒煙測試、回歸測試等固定組合
  - 一次執行多個跨專案 / 跨 FEATURE 的測試
  - 搭配排程重複執行同一批測試

node_ids_json 儲存 JSON array 的節點 id；執行時會遞迴展開每個節點底下的 TESTCASE。
"""
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class TestRound(Base):
    __tablename__ = "test_rounds"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    # JSON array，例：'["<uuid1>", "<uuid2>"]'
    node_ids_json: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # 預設執行環境（docker / local）；立即執行時可被 override
    execution_mode: Mapped[str] = mapped_column(String(16), default="docker", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )
