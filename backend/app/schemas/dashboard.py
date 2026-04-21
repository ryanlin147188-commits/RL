from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class MetricsResponse(BaseModel):
    total_executions: int
    pass_rate: float          # 0.0 ~ 100.0
    total_testcases: int
    avg_duration_ms: int
    active_runs: int


class ChartDataPoint(BaseModel):
    label: str                              # fallback / 舊相容欄位
    passed: int
    failed: int
    created_at: Optional[datetime] = None   # 原始時間（UTC naive），前端在本地時區格式化


class ChartsResponse(BaseModel):
    # 圓餅圖
    status_summary: dict[str, int]   # {"passed": X, "failed": Y}
    # 長條圖（最近 N 次執行，目前 N=5 以對應前端「近五次執行趨勢」標題）
    trend: list[ChartDataPoint]
