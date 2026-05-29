"""export_report_pdf tool — 非同步匯出測試報告(目前產 HTML,Phase 2 換真正 PDF)。

對齊 [tasks.export_tasks.export_report_task](../../../tasks/export_tasks.py)。
派 Celery task 後立刻回 task_id,**不等結果** — 對話框後續可透過 /ws 訂閱進度,
或請使用者去 reports 頁面查下載連結。

設計:
* is_async=True — agent_service 看到 metadata.task_id 會把它寫進 message,
  前端自動連 WebSocket 訂閱進度。
* requires_confirmation=False — 純讀資料 → 寫 S3 一份檔案,非 destructive。
* concurrency_limit_per_user=2 — 避免 LLM 在 loop 內猛派匯出。
"""
from __future__ import annotations

import json
from typing import Any

from app.agent.tools.base import Tool, ToolContext, ToolResult
from app.auth.permissions_catalog import P
from app.auth.tenant import TenantQuery
from app.models.execution_report import ExecutionReport


class ExportReportPdfTool(Tool):
    name = "export_report_pdf"
    description = (
        "匯出測試報告(HTML 格式,可在瀏覽器列印成 PDF)。**非同步**:派出後立即"
        "回 task_id,使用者去 reports 頁面看下載連結。"
        " Phase 1 產 HTML,Phase 2 會升級為真正 PDF(reportlab / weasyprint)。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "report_id": {
                "type": "string",
                "description": "目標 ExecutionReport UUID",
            },
            "output_format": {
                "type": "string",
                "enum": ["html", "pdf"],
                "description": "預設 html;pdf 目前 fallback 走 html",
            },
        },
        "required": ["report_id"],
        "additionalProperties": False,
    }
    casbin_permission = P.REPORT_READ
    requires_confirmation = False
    is_async = True
    concurrency_limit_per_user = 2

    async def execute(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        report_id = (kwargs.get("report_id") or "").strip()
        if not report_id:
            return ToolResult.fail(
                "missing_report_id", llm_visible="report_id 必填。"
            )
        output_format = (kwargs.get("output_format") or "html").lower()

        # tenant scope:確認 report 在 caller org 範圍內
        stmt = TenantQuery.for_(ExecutionReport).where(
            ExecutionReport.id == report_id
        )
        report = (await ctx.db.execute(stmt)).scalar_one_or_none()
        if report is None:
            return ToolResult.fail(
                "report_not_found",
                llm_visible=f"report {report_id} 不存在或不在你的存取範圍內。",
            )

        # 派 Celery task(lazy import 避免測試環境沒 celery 啟動就 crash)
        try:
            from tasks.export_tasks import export_report_task
        except ImportError as exc:
            return ToolResult.fail(
                f"celery_unavailable: {exc}",
                llm_visible="匯出服務暫時無法使用(Celery worker 未就緒)。",
            )

        async_result = export_report_task.apply_async(
            kwargs={"report_id": report_id, "output_format": output_format},
        )
        task_id = async_result.id

        payload = {
            "status": "queued",
            "task_id": task_id,
            "report_id": report_id,
            "format": output_format,
            "hint": (
                "報告產生中,完成後請到 /#/reports 頁面或刷新對話查看下載連結。"
            ),
        }
        # is_async=True 的 tool 必須在 metadata 內帶 task_id
        return ToolResult(
            content=json.dumps(payload, ensure_ascii=False),
            metadata={"task_id": task_id, "report_id": report_id},
        )
