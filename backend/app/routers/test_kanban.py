"""Test Kanban (測試看板) read-only endpoint。

把 project 內所有 testcase 用「最近一次執行 + 是否有 open defect」derived 出
5 個 bucket(待測試 / 測試中 / 已通過 / 失敗 / 複測中),純 read,不寫資料庫。

API:
    GET /api/test-kanban?project_id=<pid>[&assignee=<u>&priority=<P0/P1/P2/P3>&q=<...>]

回傳:
    {
      "columns": {
        "todo":        [card, ...],
        "in_progress": [card, ...],
        "passed":      [card, ...],
        "failed":      [card, ...],
        "retest":      [card, ...]
      },
      "counts": { "todo": int, "in_progress": int, "passed": int, "failed": int, "retest": int }
    }
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.auth.tenant import TenantQuery
from app.database import get_db
from app.models.defect import Defect, DefectStatus
from app.models.execution_report import ExecutionReport, ReportStatus
from app.models.execution_step_log import ExecutionStepLog, StepStatus
from app.models.tree_node import LevelType, TreeNode
from app.models.user import User

router = APIRouter()

_PER_COLUMN_LIMIT = 100


def _card(
    node: TreeNode,
    latest_step: Optional[ExecutionStepLog],
    latest_report: Optional[ExecutionReport],
    defects: list[Defect],
) -> dict:
    return {
        "testcase_id": node.id,
        "code": node.id[:8],  # tree_node 沒有 code 欄位;前 8 碼當顯示用
        "title": node.name,
        "assignee": node.assigned_to,
        "linked_defects": [
            {"id": d.id, "code": d.code, "status": d.status.value, "priority": d.priority.value}
            for d in defects
        ],
        "defect_count": len(defects),
        "latest_execution": (
            {
                "id": latest_report.id if latest_report else None,
                "status": latest_step.status.value if latest_step else (
                    latest_report.status.value if latest_report else None
                ),
                "ended_at": (latest_step.created_at if latest_step else None).isoformat() if latest_step else None,
                "duration_ms": latest_step.duration_ms if latest_step else None,
            }
            if (latest_step or latest_report)
            else None
        ),
    }


def _classify(
    latest_step: Optional[ExecutionStepLog],
    latest_report: Optional[ExecutionReport],
    open_defects: list[Defect],
) -> str:
    """5 個欄的優先順序:retest > in_progress > failed > passed > todo。"""
    # 複測中:有 open defect 在 REWORK_REQUIRED 或 IN_REVIEW
    if any(d.status in (DefectStatus.REWORK_REQUIRED, DefectStatus.IN_REVIEW) for d in open_defects):
        return "retest"
    # 測試中:最新 report 還在 RUNNING(無論 step 跑到哪)
    if latest_report and latest_report.status == ReportStatus.RUNNING:
        return "in_progress"
    # passed / failed 根據最新 step status(無 step 看 report status)
    last_status = latest_step.status if latest_step else (latest_report.status if latest_report else None)
    if last_status is None:
        return "todo"
    if last_status == StepStatus.FAILED or last_status == ReportStatus.FAILED:
        return "failed"
    if last_status == StepStatus.PASSED or last_status == ReportStatus.PASSED:
        return "passed"
    return "todo"


@router.get("/test-kanban", tags=["AC · 測試看板"])
async def get_test_kanban(
    project_id: str = Query(..., description="必填:要看哪個 project 的看板"),
    assignee: Optional[str] = Query(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # 1) 抓 project 內所有 testcase node(level_type=TESTCASE)
    nodes_stmt = (
        TenantQuery.for_(TreeNode)
        .where(TreeNode.project_id == project_id)
        .where(TreeNode.level_type == LevelType.TESTCASE)
    )
    if assignee is not None:
        if assignee == "null":
            nodes_stmt = nodes_stmt.where(TreeNode.assigned_to.is_(None))
        else:
            nodes_stmt = nodes_stmt.where(TreeNode.assigned_to == assignee)
    nodes = (await db.execute(nodes_stmt)).scalars().all()
    if not nodes:
        return {"columns": {k: [] for k in ("todo", "in_progress", "passed", "failed", "retest")},
                "counts": {k: 0 for k in ("todo", "in_progress", "passed", "failed", "retest")}}

    node_ids = [n.id for n in nodes]

    # 2) 每個 testcase 的最新 step log(用 max(created_at))
    latest_step_subq = (
        select(
            ExecutionStepLog.testcase_node_id,
            func.max(ExecutionStepLog.created_at).label("last_at"),
        )
        .where(ExecutionStepLog.testcase_node_id.in_(node_ids))
        .group_by(ExecutionStepLog.testcase_node_id)
        .subquery()
    )
    latest_steps = (
        await db.execute(
            select(ExecutionStepLog)
            .join(
                latest_step_subq,
                (ExecutionStepLog.testcase_node_id == latest_step_subq.c.testcase_node_id)
                & (ExecutionStepLog.created_at == latest_step_subq.c.last_at),
            )
        )
    ).scalars().all()
    step_by_node: dict[str, ExecutionStepLog] = {s.testcase_node_id: s for s in latest_steps if s.testcase_node_id}

    # 3) 抓對應 report(只抓會用到的)
    report_ids = {s.report_id for s in latest_steps}
    report_by_id: dict[str, ExecutionReport] = {}
    if report_ids:
        rows = (
            await db.execute(select(ExecutionReport).where(ExecutionReport.id.in_(report_ids)))
        ).scalars().all()
        report_by_id = {r.id: r for r in rows}

    # 4) 抓未關閉的 defect,按 testcase 分組
    open_defects_rows = (
        await db.execute(
            select(Defect)
            .where(Defect.project_id == project_id)
            .where(Defect.linked_testcase_id.in_(node_ids))
            .where(Defect.status != DefectStatus.CLOSED)
            .order_by(desc(Defect.updated_at))
        )
    ).scalars().all()
    defects_by_node: dict[str, list[Defect]] = {}
    for d in open_defects_rows:
        if d.linked_testcase_id:
            defects_by_node.setdefault(d.linked_testcase_id, []).append(d)

    # 5) 分桶
    columns: dict[str, list[dict]] = {
        "todo": [], "in_progress": [], "passed": [], "failed": [], "retest": [],
    }
    for n in nodes:
        step = step_by_node.get(n.id)
        report = report_by_id.get(step.report_id) if step else None
        ds = defects_by_node.get(n.id, [])
        bucket = _classify(step, report, ds)
        columns[bucket].append(_card(n, step, report, ds))

    counts = {k: len(v) for k, v in columns.items()}
    # truncate per column
    for k in columns:
        columns[k] = columns[k][:_PER_COLUMN_LIMIT]
    return {"columns": columns, "counts": counts}
