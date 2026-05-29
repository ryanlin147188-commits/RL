"""Celery task — 把 ExecutionReport 渲染成可下載的 HTML 報告。

Phase 1 用 HTML(瀏覽器列印成 PDF),Phase 2 可換 reportlab / weasyprint 真正生 PDF。
本檔不引入新 dependency — 純 Python f-string 組 HTML 字串,寫進 SeaweedFS S3。

設計:
* Celery task name:``tasks.export_tasks.export_report``(底線分隔)
* 輸入:report_id(必填)+ format(html / pdf;pdf 目前 fallback 走 html)
* 輸出:task return dict 含 ``download_url``(public artifact 路徑)
* 失敗:raise → autoretry 1 次,二次失敗 task ack as failed
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from celery.utils.log import get_task_logger

from app.db.sync_session import SessionLocal
from app.models.execution_report import ExecutionReport, ReportStatus
from app.models.execution_step_log import ExecutionStepLog, StepStatus
from app.services.storage_service import save_bytes
from tasks.celery_app import celery_app

logger = get_task_logger(__name__)


def _safe_text(value: Any) -> str:
    """HTML escape + 空值處理。"""
    if value is None:
        return ""
    s = str(value)
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _render_step_row(step: ExecutionStepLog) -> str:
    status = step.status.value if hasattr(step.status, "value") else str(step.status)
    color = {"PASSED": "#28a745", "FAILED": "#dc3545", "SKIPPED": "#ffc107"}.get(
        status, "#6c757d"
    )
    error = _safe_text(step.error_message)[:500] if step.error_message else ""
    screenshot = (
        f'<a href="{_safe_text(step.post_screenshot_url)}" target="_blank">screenshot</a>'
        if step.post_screenshot_url
        else ""
    )
    return f"""
        <tr>
            <td>{step.step_index}</td>
            <td><span style="color:{color};font-weight:bold;">{status}</span></td>
            <td>{step.duration_ms} ms</td>
            <td>{error}</td>
            <td>{screenshot}</td>
        </tr>
    """


def _render_report_html(
    report: ExecutionReport, steps: list[ExecutionStepLog]
) -> str:
    """組成單一報告的 HTML。CSS 內嵌,獨立可寄出 / 列印成 PDF。"""
    total = report.total_cases or 0
    passed = report.passed_cases or 0
    failed = report.failed_cases or 0
    pass_rate = round(passed / total * 100, 1) if total else 0.0
    status_val = (
        report.status.value if hasattr(report.status, "value") else str(report.status)
    )
    created_str = (
        report.created_at.strftime("%Y-%m-%d %H:%M:%S")
        if report.created_at
        else "—"
    )

    step_rows = "".join(_render_step_row(s) for s in steps[:200])  # 上限 200 step 避免 HTML 過大
    truncated_note = ""
    if len(steps) > 200:
        truncated_note = f'<p style="color:#888;">⚠ 共 {len(steps)} 步,僅顯示前 200 步。</p>'

    return f"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8">
<title>測試報告 {_safe_text(report.id)[:8]}</title>
<style>
  body {{ font-family: -apple-system, system-ui, "Helvetica Neue", sans-serif; margin: 40px; }}
  h1 {{ border-bottom: 2px solid #333; padding-bottom: 8px; }}
  table {{ width: 100%; border-collapse: collapse; margin-top: 16px; }}
  th, td {{ border: 1px solid #ddd; padding: 8px; text-align: left; vertical-align: top; }}
  th {{ background: #f5f5f5; }}
  .summary {{ display: flex; gap: 24px; margin: 16px 0; }}
  .summary div {{ padding: 12px 18px; background: #f9f9f9; border-radius: 6px; }}
  .pass {{ color: #28a745; font-weight: bold; }}
  .fail {{ color: #dc3545; font-weight: bold; }}
  @media print {{ body {{ margin: 20px; }} }}
</style>
</head>
<body>
<h1>測試執行報告</h1>
<div class="summary">
  <div><strong>Report ID:</strong> {_safe_text(report.id)}</div>
  <div><strong>狀態:</strong> {_safe_text(status_val)}</div>
  <div><strong>建立時間:</strong> {created_str}</div>
  <div><strong>執行模式:</strong> {_safe_text(report.execution_mode)}</div>
</div>
<div class="summary">
  <div>總 case: <strong>{total}</strong></div>
  <div class="pass">通過: <strong>{passed}</strong></div>
  <div class="fail">失敗: <strong>{failed}</strong></div>
  <div><strong>通過率: {pass_rate}%</strong></div>
  <div><strong>總耗時:</strong> {report.duration_ms or 0} ms</div>
</div>

<h2>步驟明細</h2>
{truncated_note}
<table>
  <thead>
    <tr><th>#</th><th>狀態</th><th>耗時</th><th>錯誤訊息</th><th>截圖</th></tr>
  </thead>
  <tbody>
    {step_rows or '<tr><td colspan="5">無步驟資料</td></tr>'}
  </tbody>
</table>

<p style="color:#888;margin-top:32px;font-size:12px;">
  由 Kapito 平台自動產生 — {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC
</p>
</body>
</html>"""


@celery_app.task(
    bind=True,
    name="tasks.export_tasks.export_report",
    max_retries=1,
    default_retry_delay=30,
)
def export_report_task(
    self,
    *,
    report_id: str,
    output_format: str = "html",
) -> dict[str, Any]:
    """產生 ExecutionReport 的可下載報告,存 S3 並回 download_url。

    output_format:
        * ``html`` — 預設,純 HTML(瀏覽器列印成 PDF)
        * ``pdf`` — Phase 1 暫退回 html;之後可換 reportlab / weasyprint

    回傳 dict 結構:
        {
            "status": "ok" | "fail",
            "report_id": "...",
            "download_url": "/artifacts/results/exports/<file>",
            "format": "html",
            "size_bytes": 1234,
        }
    """
    fmt = (output_format or "html").lower()
    if fmt not in ("html", "pdf"):
        return {
            "status": "fail",
            "report_id": report_id,
            "error": f"unsupported_format: {fmt!r}(支援 html / pdf)",
        }
    if fmt == "pdf":
        logger.warning(
            "export_report PDF format not yet implemented; falling back to HTML"
        )
        fmt = "html"

    with SessionLocal() as db:
        report = db.get(ExecutionReport, report_id)
        if report is None:
            return {
                "status": "fail",
                "report_id": report_id,
                "error": "report_not_found",
            }

        # 拉所有 step log(by step_index 排序)
        from sqlalchemy import select

        stmt = (
            select(ExecutionStepLog)
            .where(ExecutionStepLog.report_id == report_id)
            .order_by(ExecutionStepLog.step_index)
        )
        steps = list(db.execute(stmt).scalars().all())

        html = _render_report_html(report, steps)
        html_bytes = html.encode("utf-8")

        # S3 key:exports/<report_id>-<timestamp>.html
        ts = datetime.utcnow().strftime("%Y%m%d%H%M%S")
        key = f"exports/{report_id}-{ts}.html"
        try:
            url = save_bytes(
                html_bytes,
                key=key,
                bucket="results",
                content_type="text/html; charset=utf-8",
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("export_report S3 upload failed: %s", exc)
            return {
                "status": "fail",
                "report_id": report_id,
                "error": f"upload_failed: {exc}",
            }

        return {
            "status": "ok",
            "report_id": report_id,
            "download_url": url,
            "format": fmt,
            "size_bytes": len(html_bytes),
            "step_count": len(steps),
        }
