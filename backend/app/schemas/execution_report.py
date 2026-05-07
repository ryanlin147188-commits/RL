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
    # 單選(舊呼叫端)。新呼叫端可用 node_ids,兩者擇一,皆有時 node_ids 優先。
    node_id: Optional[str] = None
    # 多選(批次執行,清單頁「執行已選」用)。每個 id 可以是 folder / scenario
    # 容器或 leaf testcase;後端會展開為 leaf 並去重,跨 project 會被擋。
    node_ids: list[str] = []
    trigger_type: str = "Manual"
    # 執行環境："docker"(Celery 容器)或 "local"(本機標記)。預設 docker。
    execution_mode: str = "docker"
    # 是否把測試案例的 DDT 全部列依序執行。預設 False(只跑一次;只用第一列當變數)
    ddt_expand: bool = False
    # 是否啟用 Trace(軌跡追蹤)+ Video(錄影)收集。預設 True;
    # 關閉可加快執行速度並節省磁碟(不會產生 trace.zip / .webm)。
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
    # 觸發執行的節點 id（前端用來顯示「這次是在跑哪個測試案例」）
    source_node_id: Optional[str] = None
    # 由清單端點動態補上：source_node_id 對應的節點 title（該節點本身）與祖先鏈 PLATFORM / PAGE name。
    # 用途：報告清單直接顯示「平台 / 頁面 / 測試案例」三欄，不需前端再去樹狀查詢。
    source_title: Optional[str] = None
    source_platform: Optional[str] = None
    source_page: Optional[str] = None
    enable_recording: bool = True
    test_version_id: Optional[str] = None
    created_at: datetime


# ── 報告詳情（不含步驟，步驟由 /steps 端點取得）──────────────
class ReportDetailResponse(ReportListItem):
    pass


# ── 報告步驟列表 ─────────────────────────────────────────────
class ReportStepsResponse(BaseModel):
    report_id: str
    total_steps: int
    steps: list[StepLogResponse]
