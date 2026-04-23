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
    # 執行環境："docker"（Celery 容器）或 "local"（本機標記）。預設 docker。
    execution_mode: str = "docker"
    # 是否把測試案例的 DDT 全部列依序執行。預設 False（只跑一次；只用第一列當變數）
    ddt_expand: bool = False
    # 是否啟用 Trace（軌跡追蹤）+ Video（錄影）收集。預設 True；
    # 關閉可加快執行速度並節省磁碟（不會產生 trace.zip / .webm）。
    enable_recording: bool = True


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
    # Trace / Video（案例級欄位 trace_url、video_url 僅在每個 case/round 第一個 step 上才有值）
    trace_url: Optional[str] = None
    video_url: Optional[str] = None
    step_video_url: Optional[str] = None
    # Screenshot diff（只 AssertScreenshotMatch step 才有值）
    screenshot_baseline_url: Optional[str] = None
    screenshot_diff_url: Optional[str] = None
    screenshot_diff_pct: Optional[float] = None


# ── 報告清單（摘要）─────────────────────────────────────────
class ReportListItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    task_id: Optional[str]
    project_id: str
    trigger_type: str
    execution_mode: str = "docker"
    status: ReportStatus
    duration_ms: int
    total_cases: int
    passed_cases: int
    failed_cases: int
    # step-level 統計（不存於 ExecutionReport，由清單端點動態聚合）
    passed_steps: int = 0
    failed_steps: int = 0
    enable_recording: bool = True
    created_at: datetime


# ── 報告詳情（不含步驟，步驟由 /steps 端點取得）──────────────
class ReportDetailResponse(ReportListItem):
    pass


# ── 報告步驟列表 ─────────────────────────────────────────────
class ReportStepsResponse(BaseModel):
    report_id: str
    total_steps: int
    steps: list[StepLogResponse]
