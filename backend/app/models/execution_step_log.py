import enum
import uuid
from typing import Any, Optional

from sqlalchemy import JSON, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base


class StepStatus(str, enum.Enum):
    RUNNING = "RUNNING"
    PASSED = "PASSED"
    FAILED = "FAILED"


class ExecutionStepLog(Base):
    __tablename__ = "execution_steps_log"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    report_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("execution_reports.id", ondelete="CASCADE"), nullable=False
    )
    # 刪除 tree_nodes 時將此欄位設為 NULL（保留歷史紀錄）
    testcase_node_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("tree_nodes.id", ondelete="SET NULL"), nullable=True
    )
    step_index: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[StepStatus] = mapped_column(
        Enum(StepStatus), default=StepStatus.RUNNING
    )
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # ── UI 截圖欄位 ─────────────────────────────────────────────
    pre_screenshot_url: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    post_screenshot_url: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    # 紅框座標 {"top":"35%","left":"25%","width":"50%","height":"10%"}
    target_highlight_json: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)

    # ── API 測試欄位 ────────────────────────────────────────────
    req_payload_json: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)
    res_payload_json: Mapped[Optional[Any]] = mapped_column(JSON, nullable=True)

    # ── Trace / Video（僅在每個案例/DDT round 的「第一個步驟」填入；其餘 step 的
    #    case 級欄位為 NULL；step_video_url 則為該步驟的影片切片）─────────────
    # Playwright trace.zip 對外 URL（可用 playwright show-trace 或 trace.playwright.dev 開啟）
    trace_url: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    # 案例完整錄影（.webm）對外 URL
    video_url: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    # 該步驟切片錄影（.webm）對外 URL
    step_video_url: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)

    report: Mapped["ExecutionReport"] = relationship(
        "ExecutionReport", back_populates="steps", lazy="noload"
    )
