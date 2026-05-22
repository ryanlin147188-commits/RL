"""Test Kanban (測試看板) endpoint。

把 project 內所有 testcase 依 ``tree_nodes.work_status`` 分到 5 個 bucket
(NEW=待測試 / IN_PROGRESS=測試中 / PASSED=已通過 / FAILED=失敗 / RETEST=複測中)。

v1.1.9 改 derived-only → 真欄位 storage:user 可以在前端拖拽改變 testcase
的 work_status(PATCH /api/nodes/{id} 帶 work_status),server 只負責讀。

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
from app.models.tree_node import LevelType, TreeNode, WorkStatus  # noqa: F401  WorkStatus used by tests / future filters
from app.models.user import User

router = APIRouter()

_PER_COLUMN_LIMIT = 100


def _card(
    node: TreeNode,
    defects: list[Defect],
) -> dict:
    return {
        "testcase_id": node.id,
        "code": node.id[:8],  # tree_node 沒有 code 欄位;前 8 碼當顯示用
        "title": node.name,
        "assignee": node.assigned_to,
        "work_status": node.work_status,
        "linked_defects": [
            {"id": d.id, "code": d.code, "status": d.status.value, "priority": d.priority.value}
            for d in defects
        ],
        "defect_count": len(defects),
    }


# work_status enum value → kanban column key
_WORK_STATUS_TO_COLUMN = {
    "NEW":         "todo",
    "IN_PROGRESS": "in_progress",
    "PASSED":      "passed",
    "FAILED":      "failed",
    "RETEST":      "retest",
}


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

    # 抓未關閉的 defect 按 testcase 分組(顯示卡片上 🐞 badge 用)
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

    # 分桶:直接讀 work_status,沒 mapping 就退到 todo(NEW)
    columns: dict[str, list[dict]] = {
        "todo": [], "in_progress": [], "passed": [], "failed": [], "retest": [],
    }
    for n in nodes:
        bucket = _WORK_STATUS_TO_COLUMN.get(n.work_status or "NEW", "todo")
        ds = defects_by_node.get(n.id, [])
        columns[bucket].append(_card(n, ds))

    counts = {k: len(v) for k, v in columns.items()}
    for k in columns:
        columns[k] = columns[k][:_PER_COLUMN_LIMIT]
    return {"columns": columns, "counts": counts}
