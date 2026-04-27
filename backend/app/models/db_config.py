"""DB connection 設定持久化 — 取代原本只存於前端 localStorage 的 DB 設定。

每組設定對應一個資料庫連線(MySQL / PostgreSQL / MSSQL / Oracle / MongoDB
/ Redis / SQLite),會被注入成 Robot suite variable `&{DB_<name>}` 給
`Db.*` 步驟使用。

password 以 Fernet 加密儲存(沿用 `app.auth.crypto.EncryptedString`)。
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.auth.crypto import EncryptedString

from .base import Base


class DbConfig(Base):
    __tablename__ = "db_configs"
    __table_args__ = (
        # 同一專案內 name 不可重複(name 會變成 Robot 變數 &{DB_<name>})
        UniqueConstraint("project_id", "name", name="uq_dbconfig_project_name"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    organization_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    project_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=True, index=True,
    )
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    # mysql / postgresql / mssql / oracle / mongodb / redis / sqlite
    db_type: Mapped[str] = mapped_column(String(20), nullable=False)
    host: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    port: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    database: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    username: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    # password Fernet 加密;讀寫透明,DB 落地是密文
    password_encrypted: Mapped[Optional[str]] = mapped_column(EncryptedString(), nullable=True)
    # 進階:charset/SSL 等,單一字串自由輸入
    extra_options: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    # 自訂 DSN/連線字串(填了會覆蓋上方欄位)
    custom_dsn: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )
