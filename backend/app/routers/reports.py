from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.execution_report import ExecutionReport, ReportStatus
from app.models.execution_step_log import ExecutionStepLog
from app.schemas.dashboard import ChartDataPoint, ChartsResponse, MetricsResponse
from app.schemas.execution_report import (
    PaginatedResponse,
    ReportDetailResponse,
    ReportListItem,
    ReportStepsResponse,
    StepLogResponse,
)

router = APIRouter()


# 11. GET /api/dashboard/metrics
@router.get("/dashboard/metrics", response_model=MetricsResponse)
async def get_metrics(
    project_id: str = Query(..., description="專案 ID"),
    db: AsyncSession = Depends(get_db),
):
    total = await db.scalar(
        select(func.count()).where(ExecutionReport.project_id == project_id)
    ) or 0

    row = (
        await db.execute(
            select(
                func.sum(ExecutionReport.passed_cases),
                func.sum(ExecutionReport.total_cases),
                func.avg(ExecutionReport.duration_ms),
            ).where(
                ExecutionReport.project_id == project_id,
                ExecutionReport.status != ReportStatus.RUNNING,
            )
        )
    ).one()

    total_passed = int(row[0] or 0)
    total_cases_sum = int(row[1] or 1)
    avg_duration = int(row[2] or 0)
    pass_rate = round(total_passed / total_cases_sum * 100, 1)

    active = await db.scalar(
        select(func.count()).where(
            ExecutionReport.project_id == project_id,
            ExecutionReport.status == ReportStatus.RUNNING,
        )
    ) or 0

    tc_count = await db.scalar(
        select(func.count(ExecutionStepLog.testcase_node_id.distinct()))
        .join(ExecutionReport, ExecutionStepLog.report_id == ExecutionReport.id)
        .where(ExecutionReport.project_id == project_id)
    ) or 0

    return MetricsResponse(
        total_executions=total,
        pass_rate=pass_rate,
        total_testcases=tc_count,
        avg_duration_ms=avg_duration,
        active_runs=active,
    )


# 12. GET /api/dashboard/charts
@router.get("/dashboard/charts", response_model=ChartsResponse)
async def get_charts(
    project_id: str = Query(...),
    db: AsyncSession = Depends(get_db),
):
    passed_sum = await db.scalar(
        select(func.sum(ExecutionReport.passed_cases)).where(
            ExecutionReport.project_id == project_id,
            ExecutionReport.status != ReportStatus.RUNNING,
        )
    ) or 0
    failed_sum = await db.scalar(
        select(func.sum(ExecutionReport.failed_cases)).where(
            ExecutionReport.project_id == project_id,
            ExecutionReport.status != ReportStatus.RUNNING,
        )
    ) or 0

    recent_result = await db.execute(
        select(ExecutionReport)
        .where(
            ExecutionReport.project_id == project_id,
            ExecutionReport.status != ReportStatus.RUNNING,
        )
        .order_by(desc(ExecutionReport.created_at))
        .limit(10)
    )
    recent = list(reversed(recent_result.scalars().all()))

    trend = [
        ChartDataPoint(
            label=f"#{r.id[:6]}",
            passed=r.passed_cases,
            failed=r.failed_cases,
        )
        for r in recent
    ]

    return ChartsResponse(
        status_summary={"passed": int(passed_sum), "failed": int(failed_sum)},
        trend=trend,
    )


# 13. GET /api/v1/reports  （分頁）
@router.get("/reports", response_model=PaginatedResponse[ReportListItem])
async def list_reports(
    project_id: str = Query(...),
    page: int = Query(1, ge=1, description="頁碼（從 1 開始）"),
    limit: int = Query(10, ge=1, le=100, description="每頁筆數"),
    db: AsyncSession = Depends(get_db),
):
    offset = (page - 1) * limit
    total = await db.scalar(
        select(func.count()).where(ExecutionReport.project_id == project_id)
    ) or 0
    result = await db.execute(
        select(ExecutionReport)
        .where(ExecutionReport.project_id == project_id)
        .order_by(desc(ExecutionReport.created_at))
        .offset(offset)
        .limit(limit)
    )
    items = result.scalars().all()
    return PaginatedResponse(
        total=total,
        page=page,
        limit=limit,
        items=items,
    )


# 14. GET /api/v1/reports/{id}  （摘要，不含步驟）
@router.get("/reports/{report_id}", response_model=ReportDetailResponse)
async def get_report(report_id: str, db: AsyncSession = Depends(get_db)):
    """'取得單次執行摘要（統計數據，不含步驟明細）。步驟明細請呼叫 /reports/{id}/steps。"""
    report = await db.get(ExecutionReport, report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Report not found")
    return report


# 15. GET /api/v1/reports/{id}/steps  （步驟明細、截圖 URL、JSON payload）
@router.get("/reports/{report_id}/steps", response_model=ReportStepsResponse)
async def get_report_steps(report_id: str, db: AsyncSession = Depends(get_db)):
    """'取得單次執行中所有步驟的明細紀錄（包含截圖 URL 與 JSON payload）。"""
    report = await db.get(ExecutionReport, report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Report not found")

    steps_result = await db.execute(
        select(ExecutionStepLog)
        .where(ExecutionStepLog.report_id == report_id)
        .order_by(ExecutionStepLog.step_index)
    )
    steps = steps_result.scalars().all()
    return ReportStepsResponse(
        report_id=report_id,
        total_steps=len(steps),
        steps=steps,
    )
