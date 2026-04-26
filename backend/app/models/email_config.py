"""EmailConfig 電子郵件設定 ORM Model — singleton（id="default"）。

存 SMTP 連線資訊，給通知系統發信用。為了避免明文外流，未來建議 api_key / password
改用 KMS 加密；目前簡化以明文存。
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.auth.crypto import EncryptedString

from .base import Base


class EmailConfig(Base):
    __tablename__ = "email_configs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default="default")
    organization_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True, index=True,
    )
    smtp_host: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    smtp_port: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, default=587)
    smtp_user: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    # 用 Fernet 加密儲存；ORM 看到的是明文，DB 內是 "fernet:gAAAA..." 格式
    smtp_password: Mapped[Optional[str]] = mapped_column(EncryptedString(500), nullable=True)
    use_tls: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    from_address: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    from_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, default="AutoTest")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )
