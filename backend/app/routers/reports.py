from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.execution_report import ExecutionReport, ReportStatus
from app.models.execution_step_log import ExecutionStepLog
from app.models.tree_node import TreeNode
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

    # 前端 dashboard 標題為「近五次執行趨勢」，這裡 limit 對齊為 5
    recent_result = await db.execute(
        select(ExecutionReport)
        .where(
            ExecutionReport.project_id == project_id,
            ExecutionReport.status != ReportStatus.RUNNING,
        )
        .order_by(desc(ExecutionReport.created_at))
        .limit(5)
    )
    # 由新到舊呈現（最新的執行在最右側／最前面）
    recent = recent_result.scalars().all()

    # 趨勢圖以「步驟層級」聚合 PASSED / FAILED 數，
    # 讓部分通過的案例也能顯示綠色，而不是整支報告只看 case-level 的全紅／全綠。
    trend: list[ChartDataPoint] = []
    if recent:
        step_rows = (
            await db.execute(
                select(
                    ExecutionStepLog.report_id,
                    ExecutionStepLog.status,
                    func.count(ExecutionStepLog.id),
                )
                .where(ExecutionStepLog.report_id.in_([r.id for r in recent]))
                .group_by(ExecutionStepLog.report_id, ExecutionStepLog.status)
            )
        ).all()
        step_agg: dict[str, dict[str, int]] = {}
        for rid, status, cnt in step_rows:
            d = step_agg.setdefault(rid, {"PASSED": 0, "FAILED": 0})
            key = getattr(status, "value", str(status))
            if key in d:
                d[key] += int(cnt)

        for r in recent:
            agg = step_agg.get(r.id, {"PASSED": 0, "FAILED": 0})
            # fallback：若該報告無 step log（例如只有 case-level 結果），
            # 退回使用 case 層級數字，避免出現空白柱。
            passed = agg["PASSED"] or r.passed_cases
            failed = agg["FAILED"] or r.failed_cases
            trend.append(
                ChartDataPoint(
                    label=f"#{r.id[:6]}",
                    passed=passed,
                    failed=failed,
                    created_at=r.created_at,
                )
            )

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

    # 補上 step-level 統計（passed_steps / failed_steps），給前端列表顯示
    item_dicts: list[dict] = []
    if items:
        report_ids = [r.id for r in items]
        step_rows = (
            await db.execute(
                select(
                    ExecutionStepLog.report_id,
                    ExecutionStepLog.status,
                    func.count(ExecutionStepLog.id),
                )
                .where(ExecutionStepLog.report_id.in_(report_ids))
                .group_by(ExecutionStepLog.report_id, ExecutionStepLog.status)
            )
        ).all()
        agg: dict[str, dict[str, int]] = {}
        for rid, st, cnt in step_rows:
            agg.setdefault(rid, {})[str(st.value if hasattr(st, "value") else st)] = int(cnt)

        # 批次查「這次執行的觸發節點 title」：source_node_id → TreeNode.name。
        # 報告分頁通常只有 10-20 筆，單次 IN 查詢即可。
        src_ids = {r.source_node_id for r in items if r.source_node_id}
        node_name_map: dict[str, str] = {}
        if src_ids:
            nodes = (
                await db.execute(
                    select(TreeNode.id, TreeNode.name).where(TreeNode.id.in_(src_ids))
                )
            ).all()
            node_name_map = {nid: nm for nid, nm in nodes}

        # 若 source_node_id 指向 TESTCASE 節點，亦回傳案例標題；若指向 FEATURE / PAGE 等中繼層級，
        # 前端會一樣顯示；實際 source 可能是被 testcase 以外的層級觸發（例如整個 PAGE 跑）。
        for r in items:
            d = ReportListItem.model_validate(r).model_dump()
            stats = agg.get(r.id, {})
            d["passed_steps"] = stats.get("PASSED", 0)
            d["failed_steps"] = stats.get("FAILED", 0)
            if r.source_node_id and r.source_node_id in node_name_map:
                d["source_title"] = node_name_map[r.source_node_id]
            item_dicts.append(d)
    return PaginatedResponse(
        total=total,
        page=page,
        limit=limit,
        items=item_dicts or [ReportListItem.model_validate(r).model_dump() for r in items],
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


# 16. DELETE /api/reports/{id}  刪除單筆執行報告（連同步驟明細）
@router.delete("/reports/{report_id}", status_code=204)
async def delete_report(report_id: str, db: AsyncSession = Depends(get_db)):
    """刪除單筆執行報告。ExecutionStepLog 已設 cascade=delete-orphan，會一起刪除。"""
    report = await db.get(ExecutionReport, report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Report not found")
    await db.delete(report)
    await db.commit()
    return None


# 17. POST /api/reports/delete-batch  批次刪除執行報告
class BatchDeleteRequest(BaseModel):
    ids: list[str]


class BatchDeleteResponse(BaseModel):
    deleted: int
    not_found: list[str]


@router.post("/reports/delete-batch", response_model=BatchDeleteResponse)
async def delete_reports_batch(
    payload: BatchDeleteRequest, db: AsyncSession = Depends(get_db)
):
    """批次刪除多筆執行報告。回傳實際刪除筆數與不存在的 id 清單。"""
    if not payload.ids:
        return BatchDeleteResponse(deleted=0, not_found=[])
    result = await db.execute(
        select(ExecutionReport).where(ExecutionReport.id.in_(payload.ids))
    )
    reports = result.scalars().all()
    found_ids = {r.id for r in reports}
    not_found = [i for i in payload.ids if i not in found_ids]
    for r in reports:
        await db.delete(r)
    await db.commit()
    return BatchDeleteResponse(deleted=len(reports), not_found=not_found)
