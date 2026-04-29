"""
錄製功能 ORM：使用 robotframework-browser / Playwright codegen
紀錄使用者本機產生的 codegen Python 腳本與 trace.zip 檔案。
"""
from datetime import datetime

from sqlalchemy import DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.auth.tenant import TenantScoped
from .base import Base


class RecordingSession(TenantScoped, Base):
    """瀏覽器錄製階段。腳本與 trace.zip 由前端使用者本機 codegen 後上傳。"""

    __tablename__ = "recording_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    target_url: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="PENDING"
    )  # PENDING / UPLOADED
    script_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    trace_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # 相對 PIC_FOLDER 的路徑，例如 recordings/<id>/trace.zip
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
