from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.auth.project_membership import ensure_project_member
from app.auth.scope import ensure_project_in_scope, scope_by_project
from app.database import get_db
from app.models.execution_report import ExecutionReport, ReportStatus
from app.models.execution_step_log import ExecutionStepLog
from app.models.review import ReviewableEntityType
from app.models.tree_node import TreeNode
from app.models.user import User
from app.services import review_service
from app.services.artifact_urls import sign_artifact_url
from app.schemas.dashboard import ChartDataPoint, ChartsResponse, MetricsResponse
from app.schemas.execution_report import (
    PaginatedResponse,
    ReportDetailResponse,
    ReportListItem,
    ReportStepsResponse,
    StepLogResponse,
)

router = APIRouter()


_STEP_ARTIFACT_URL_FIELDS = (
    "pre_screenshot_url",
    "post_screenshot_url",
    "trace_url",
    "video_url",
    "step_video_url",
    "screenshot_baseline_url",
    "screenshot_diff_url",
)


def _step_with_signed_artifacts(step: ExecutionStepLog) -> dict:
    data = StepLogResponse.model_validate(step).model_dump()
    for field in _STEP_ARTIFACT_URL_FIELDS:
        data[field] = sign_artifact_url(data.get(field))
    return data


# 11. GET /api/dashboard/metrics
@router.get(
    "/dashboard/metrics",
    response_model=MetricsResponse,
    dependencies=[Depends(ensure_project_member)],
)
async def get_metrics(
    project_id: str = Query(..., description="專案 ID"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await ensure_project_in_scope(db, project_id, user, not_found_detail="Project not found")
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
@router.get(
    "/dashboard/charts",
    response_model=ChartsResponse,
    dependencies=[Depends(ensure_project_member)],
)
async def get_charts(
    project_id: str = Query(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await ensure_project_in_scope(db, project_id, user, not_found_detail="Project not found")
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
@router.get(
    "/reports",
    response_model=PaginatedResponse[ReportListItem],
    dependencies=[Depends(ensure_project_member)],
)
async def list_reports(
    project_id: str = Query(...),
    page: int = Query(1, ge=1, description="頁碼(從 1 開始)"),
    limit: int = Query(10, ge=1, le=100, description="每頁筆數"),
    test_version_id: Optional[str] = Query(None, description="只顯示某版號相關的報告"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await ensure_project_in_scope(db, project_id, user, not_found_detail="Project not found")
    offset = (page - 1) * limit
    base = select(func.count()).where(ExecutionReport.project_id == project_id)
    if test_version_id:
        base = base.where(ExecutionReport.test_version_id == test_version_id)
    total = await db.scalar(base) or 0
    list_stmt = (
        select(ExecutionReport)
        .where(ExecutionReport.project_id == project_id)
        .order_by(desc(ExecutionReport.created_at))
        .offset(offset)
        .limit(limit)
    )
    if test_version_id:
        list_stmt = list_stmt.where(ExecutionReport.test_version_id == test_version_id)
    result = await db.execute(list_stmt)
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

        # 批次查「這次執行的觸發節點 title」與其祖先鏈（PAGE / PLATFORM）。
        # 實作：先把所有 source_node_id 相關的 tree_nodes 都抓回來（含 parent_id / level_type），
        # 再以字典在記憶體裡往上走鏈。報告分頁通常只有 10-20 筆，深度 ≤ 5，負擔可忽略。
        src_ids = {r.source_node_id for r in items if r.source_node_id}
        node_meta: dict[str, dict] = {}  # id -> {name, level, parent_id}
        if src_ids:
            # 先抓 source 節點本身，再把所有 parent 抓回來（因為 tree 最多 5 層，最多 5 輪）
            pending = set(src_ids)
            seen: set[str] = set()
            for _ in range(6):
                todo = pending - seen
                if not todo:
                    break
                rows = (
                    await db.execute(
                        select(
                            TreeNode.id,
                            TreeNode.name,
                            TreeNode.level_type,
                            TreeNode.parent_id,
                        ).where(TreeNode.id.in_(todo))
                    )
                ).all()
                for nid, name, lv, pid in rows:
                    node_meta[nid] = {
                        "name": name,
                        "level": lv.value if hasattr(lv, "value") else str(lv),
                        "parent_id": pid,
                    }
                    seen.add(nid)
                    if pid:
                        pending.add(pid)

        def _resolve_ancestry(nid: str | None):
            """回傳 (self_title, platform_name, page_name)；缺值以空字串代替"""
            if not nid or nid not in node_meta:
                return "", "", ""
            chain: list[dict] = []
            cur = nid
            for _ in range(6):
                if not cur or cur not in node_meta:
                    break
                m = node_meta[cur]
                chain.append(m)
                cur = m["parent_id"]
            self_name = chain[0]["name"] if chain else ""
            plat_name = next((m["name"] for m in chain if m["level"] == "PLATFORM"), "")
            page_name = next((m["name"] for m in chain if m["level"] == "PAGE"), "")
            return self_name, plat_name, page_name

        for r in items:
            d = ReportListItem.model_validate(r).model_dump()
            stats = agg.get(r.id, {})
            d["passed_steps"] = stats.get("PASSED", 0)
            d["failed_steps"] = stats.get("FAILED", 0)
            self_title, plat_name, page_name = _resolve_ancestry(r.source_node_id)
            if self_title:
                d["source_title"] = self_title
            if plat_name:
                d["source_platform"] = plat_name
            if page_name:
                d["source_page"] = page_name
            item_dicts.append(d)
    return PaginatedResponse(
        total=total,
        page=page,
        limit=limit,
        items=item_dicts or [ReportListItem.model_validate(r).model_dump() for r in items],
    )


# 14. GET /api/v1/reports/{id}  （摘要，不含步驟）
@router.get("/reports/{report_id}", response_model=ReportDetailResponse)
async def get_report(
    report_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """取得單次執行摘要(統計數據,不含步驟明細)。步驟明細請呼叫 /reports/{id}/steps。"""
    report = await db.get(ExecutionReport, report_id)
    await ensure_project_in_scope(
        db, report.project_id if report else None, user, not_found_detail="Report not found"
    )
    return report


# 15. GET /api/v1/reports/{id}/steps  (步驟明細、截圖 URL、JSON payload)
@router.get("/reports/{report_id}/steps", response_model=ReportStepsResponse)
async def get_report_steps(
    report_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """取得單次執行中所有步驟的明細紀錄(包含截圖 URL 與 JSON payload)。"""
    report = await db.get(ExecutionReport, report_id)
    await ensure_project_in_scope(
        db, report.project_id if report else None, user, not_found_detail="Report not found"
    )

    # 以執行順序呈現:created_at(整批寫入的時間,setup 整批先寫、main 後寫)
    # 為主排序,同 testcase 內再以 step_index 排;這樣前端 bucketing 時 setup
    # 整段會在 main 之前,且每個 case 內步驟仍依編號排。
    steps_result = await db.execute(
        select(ExecutionStepLog)
        .where(ExecutionStepLog.report_id == report_id)
        .order_by(ExecutionStepLog.created_at, ExecutionStepLog.step_index)
    )
    steps = steps_result.scalars().all()
    return ReportStepsResponse(
        report_id=report_id,
        total_steps=len(steps),
        steps=[_step_with_signed_artifacts(s) for s in steps],
    )


# 16. DELETE /api/reports/{id}  刪除單筆執行報告（連同步驟明細）
@router.delete("/reports/{report_id}", status_code=204)
async def delete_report(
    report_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """刪除單筆執行報告。ExecutionStepLog 已設 cascade=delete-orphan,會一起刪除。"""
    report = await db.get(ExecutionReport, report_id)
    await ensure_project_in_scope(
        db, report.project_id if report else None, user, not_found_detail="Report not found"
    )
    await review_service.ensure_not_approved(
        db,
        entity_type=ReviewableEntityType.REPORT,
        entity_id=report_id,
        organization_id=None if user.is_superuser else user.organization_id,
    )
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
    payload: BatchDeleteRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """批次刪除多筆執行報告。回傳實際刪除筆數與不存在的 id 清單。"""
    if not payload.ids:
        return BatchDeleteResponse(deleted=0, not_found=[])
    stmt = select(ExecutionReport).where(ExecutionReport.id.in_(payload.ids))
    stmt = scope_by_project(stmt, ExecutionReport, user)
    result = await db.execute(stmt)
    reports = result.scalars().all()
    found_ids = {r.id for r in reports}
    not_found = [i for i in payload.ids if i not in found_ids]
    for r in reports:
        await db.delete(r)
    await db.commit()
    return BatchDeleteResponse(deleted=len(reports), not_found=not_found)
