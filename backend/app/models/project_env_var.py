"""專案層級環境變數（全專案共用，所有測試案例執行時都會自動注入成 Robot suite variable）。

例：name='BASE_URL'、value='https://staging.example.com'
→ 步驟欄位內可以寫 ``${BASE_URL}/login``、``${BASE_URL}/api/users`` 等等，
   執行時 Robot Framework 會自動展開。
"""
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class ProjectEnvVar(Base):
    __tablename__ = "project_env_vars"
    __table_args__ = (
        # 同一專案內變數名稱不可重複
        UniqueConstraint("project_id", "name", name="uq_envvar_project_name"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    # 變數名稱（建議 [A-Z_][A-Z0-9_]*；前端會 normalize）
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    # 變數值（任意字串；可含 URL / token / 路徑 等）
    value: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )
