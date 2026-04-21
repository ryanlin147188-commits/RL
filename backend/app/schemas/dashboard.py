from pydantic import BaseModel


class MetricsResponse(BaseModel):
    total_executions: int
    pass_rate: float          # 0.0 ~ 100.0
    total_testcases: int
    avg_duration_ms: int
    active_runs: int


class ChartDataPoint(BaseModel):
    label: str
    passed: int
    failed: int


class ChartsResponse(BaseModel):
    # 圓餅圖
    status_summary: dict[str, int]   # {"passed": X, "failed": Y}
    # 長條圖（最近 10 次執行）
    trend: list[ChartDataPoint]
