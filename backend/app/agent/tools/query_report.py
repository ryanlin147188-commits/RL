"""query_report tool — 第一個 RL tool,純讀無 destructive。

讓使用者用自然語言問:「最近 10 個失敗的測試」、「某專案的執行記錄」等。

設計:
* 直接走 ``TenantQuery.for_(ExecutionReport)``,自動套上 org filter
  (caller 的 contextvar 已在 middleware 設好)
* 回 JSON 字串給 LLM,結構化資料 LLM 解析力最好
* 限 limit ≤ 50,避免 LLM 不小心拉幾千筆把 context 撐爆
* ``casbin_permission = P.REPORT_READ``,沒這個權限的 user 連 tool spec 都不會
  被 send_message 暴露(由 guard 在 dispatch 前過濾)
"""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy import desc

from app.agent.tools.base import Tool, ToolContext, ToolResult
from app.auth.permissions_catalog import P
from app.auth.tenant import TenantQuery
from app.models.execution_report import ExecutionReport, ReportStatus


class QueryReportTool(Tool):
    name = "query_recent_reports"
    description = (
        "查詢 RL 平台最近的測試執行報告。可選擇按專案 ID、狀態(PASSED/FAILED/RUNNING)"
        "過濾。預設回最近 10 筆,最多 50 筆。回傳含 report_id、status、通過率、"
        "duration、created_at 的 JSON 陣列。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "project_id": {
                "type": "string",
                "description": "RL 專案 ID(UUID);不填則查所有授權專案",
            },
            "status_filter": {
                "type": "string",
                "enum": ["PASSED", "FAILED", "RUNNING", "ALL"],
                "description": "報告狀態過濾;ALL = 不過濾",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 50,
                "description": "回傳幾筆,預設 10",
            },
        },
        "additionalProperties": False,
    }
    casbin_permission = P.REPORT_READ
    requires_confirmation = False

    async def execute(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        project_id = kwargs.get("project_id")
        status_filter = (kwargs.get("status_filter") or "ALL").upper()
        limit = int(kwargs.get("limit") or 10)
        limit = max(1, min(limit, 50))

        stmt = TenantQuery.for_(ExecutionReport)
        if project_id:
            stmt = stmt.where(ExecutionReport.project_id == project_id)
        if status_filter != "ALL":
            try:
                status_enum = ReportStatus(status_filter)
            except ValueError:
                return ToolResult.fail(
                    f"未知 status_filter:{status_filter!r}",
                    llm_visible=(
                        f"參數錯誤:status_filter 必須是 PASSED/FAILED/RUNNING/ALL,"
                        f"收到 {status_filter!r}"
                    ),
                )
            stmt = stmt.where(ExecutionReport.status == status_enum)
        stmt = stmt.order_by(desc(ExecutionReport.created_at)).limit(limit)

        rows = (await ctx.db.execute(stmt)).scalars().all()

        items = []
        for r in rows:
            total = r.total_cases or 0
            passed = r.passed_cases or 0
            pass_rate = round(passed / total * 100, 1) if total else None
            items.append(
                {
                    "report_id": r.id,
                    "project_id": r.project_id,
                    "status": r.status.value if hasattr(r.status, "value") else str(r.status),
                    "execution_mode": r.execution_mode,
                    "total_cases": total,
                    "passed_cases": passed,
                    "failed_cases": r.failed_cases or 0,
                    "pass_rate_pct": pass_rate,
                    "duration_ms": r.duration_ms or 0,
                    "trigger_type": r.trigger_type,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                }
            )

        payload = {
            "count": len(items),
            "filters": {
                "project_id": project_id,
                "status_filter": status_filter,
                "limit": limit,
            },
            "reports": items,
        }
        return ToolResult.ok(
            json.dumps(payload, ensure_ascii=False),
            count=len(items),
        )
