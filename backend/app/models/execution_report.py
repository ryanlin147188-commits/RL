import enum
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import JSON, Boolean, DateTime, Enum, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.auth.tenant import TenantScoped
from .base import Base


class ReportStatus(str, enum.Enum):
    RUNNING = "RUNNING"
    PASSED = "PASSED"
    FAILED = "FAILED"


class ExecutionReport(TenantScoped, Base):
    __tablename__ = "execution_reports"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    # Celery task_id，用於 GET /executions/{task_id}/status 查詢
    task_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)
    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    trigger_type: Mapped[str] = mapped_column(String(50), default="Manual")
    # 執行環境：docker（Celery 容器執行）/ local（標記為本機執行）
    execution_mode: Mapped[str] = mapped_column(String(16), default="docker", nullable=False)
    status: Mapped[ReportStatus] = mapped_column(
        Enum(ReportStatus), default=ReportStatus.RUNNING
    )
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    total_cases: Mapped[int] = mapped_column(Integer, default=0)
    passed_cases: Mapped[int] = mapped_column(Integer, default=0)
    failed_cases: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    # 本機執行（execution_mode=local）的專用欄位：agent 認領後填入當下時間，作為搶鎖記號。
    # 這個欄位在 docker 模式下永遠為 NULL。
    claimed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    # 原始觸發節點(local 模式認領時用來還原 testcase_ids;docker 模式不依賴此欄位)
    source_node_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    # v1.2 批次執行(node_ids)時保留多選清單;agent 認領時優先讀這個欄位重展開,
    # 沒有時才退回 source_node_id 單選相容。
    source_node_ids: Mapped[Optional[list[str]]] = mapped_column(JSON, nullable=True)
    # 是否把測試案例的 DDT 全部列依序執行。False 時只使用第一列當變數來源，整體只跑一次。
    ddt_expand: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # 是否啟用 Trace（軌跡追蹤，trace.zip）與 Video（錄影）收集。預設 True；關閉可大幅降低執行時間與磁碟用量。
    enable_recording: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # 反向關聯到 TestVersion(可空;若版號被刪 → set null)
    test_version_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("test_versions.id", ondelete="SET NULL"), nullable=True, index=True,
    )

    project: Mapped["Project"] = relationship(
        "Project", back_populates="execution_reports", lazy="noload"
    )
    steps: Mapped[list["ExecutionStepLog"]] = relationship(
        "ExecutionStepLog",
        back_populates="report",
        cascade="all, delete-orphan",
        lazy="noload",
    )
