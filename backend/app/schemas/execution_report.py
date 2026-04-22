from datetime import datetime
from typing import Any, Generic, Optional, TypeVar

from pydantic import BaseModel, ConfigDict

from app.models.execution_report import ReportStatus
from app.models.execution_step_log import StepStatus

T = TypeVar("T")


# ── 通用分頁包裝 ─────────────────────────────────────────────
class PaginatedResponse(BaseModel, Generic[T]):
    total: int
    page: int
    limit: int
    items: list[T]


# ── 觸發執行 ─────────────────────────────────────────────────
class ExecutionRunRequest(BaseModel):
    node_id: str
    trigger_type: str = "Manual"


class ExecutionRunResponse(BaseModel):
    task_id: str
    report_id: str
    message: str


# ── 執行狀態（輪詢用）───────────────────────────────────────
class ExecutionStatusResponse(BaseModel):
    task_id: str
    report_id: str
    status: ReportStatus
    total_cases: int
    passed_cases: int
    failed_cases: int
    progress: float   # 0.0 ~ 1.0，已完成案例 / 總案例


# ── 步驟詳細 ─────────────────────────────────────────────────
class StepLogResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    report_id: str
    testcase_node_id: Optional[str]
    step_index: int
    status: StepStatus
    duration_ms: int
    error_message: Optional[str]
    pre_screenshot_url: Optional[str]
    post_screenshot_url: Optional[str]
    target_highlight_json: Any
    req_payload_json: Any
    res_payload_json: Any


# ── 報告清單（摘要）─────────────────────────────────────────
class ReportListItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    task_id: Optional[str]
    project_id: str
    trigger_type: str
    status: ReportStatus
    duration_ms: int
    total_cases: int
    passed_cases: int
    failed_cases: int
    # step-level 統計（不存於 ExecutionReport，由清單端點動態聚合）
    passed_steps: int = 0
    failed_steps: int = 0
    created_at: datetime


# ── 報告詳情（不含步驟，步驟由 /steps 端點取得）──────────────
class ReportDetailResponse(ReportListItem):
    pass


# ── 報告步驟列表 ─────────────────────────────────────────────
class ReportStepsResponse(BaseModel):
    report_id: str
    total_steps: int
    steps: list[StepLogResponse]
