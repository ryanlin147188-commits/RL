"""query_audit_log tool — 唯讀,給 devops-debug skill 用。

對齊 GET /api/audit-logs(superuser-only)。Audit log 含跨 org 敏感操作,
endpoint 限 superuser,tool 也對齊這個限制。

由於 audit log 可能很大(每個 API request 一筆),limit 預設 50,最高 200。
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import desc, select

from app.agent.tools.base import Tool, ToolContext, ToolResult
from app.models.audit_log import AuditLog


def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            dt = dt.astimezone(tz=None).replace(tzinfo=None)
        return dt
    except ValueError:
        return None


class QueryAuditLogTool(Tool):
    name = "query_audit_log"
    description = (
        "查詢系統審計紀錄(誰、何時、改了什麼)。**只 superuser 可用**。"
        " 可按 username / entity_type / entity_id / method / status_code 範圍 / "
        " 時間範圍篩選。預設回最近 50 筆,最多 200 筆。"
        " 適合追資料修改根因、查 API 失敗集中點、看 user 操作軌跡。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "username": {
                "type": "string",
                "description": "篩選操作者 username",
            },
            "entity_type": {
                "type": "string",
                "description": "篩選實體類型(testcase / project / defect / ...)",
            },
            "entity_id": {
                "type": "string",
                "description": "篩選特定實體 ID(常配 entity_type 一起用)",
            },
            "method": {
                "type": "string",
                "enum": ["GET", "POST", "PUT", "PATCH", "DELETE"],
                "description": "HTTP method",
            },
            "status_min": {
                "type": "integer",
                "minimum": 100,
                "maximum": 599,
                "description": "status code 下限",
            },
            "status_max": {
                "type": "integer",
                "minimum": 100,
                "maximum": 599,
                "description": "status code 上限(例如 status_min=500 找錯誤)",
            },
            "start_date": {
                "type": "string",
                "description": "起始時間 ISO 8601(例 2026-05-22T00:00:00)",
            },
            "end_date": {
                "type": "string",
                "description": "結束時間 ISO 8601",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 200,
                "description": "回傳幾筆,預設 50",
            },
        },
        "additionalProperties": False,
    }
    casbin_permission = None  # 在 execute 內檢查 superuser

    async def execute(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        if not ctx.user.is_superuser:
            return ToolResult.fail(
                "superuser_required",
                llm_visible=(
                    "查詢 audit log 需要 superuser 權限"
                    "(你現在的帳號不是 superuser)。"
                ),
            )

        username = (kwargs.get("username") or "").strip() or None
        entity_type = (kwargs.get("entity_type") or "").strip() or None
        entity_id = (kwargs.get("entity_id") or "").strip() or None
        method = (kwargs.get("method") or "").strip().upper() or None
        status_min = kwargs.get("status_min")
        status_max = kwargs.get("status_max")
        start_date = _parse_iso(kwargs.get("start_date"))
        end_date = _parse_iso(kwargs.get("end_date"))
        limit = int(kwargs.get("limit") or 50)
        limit = max(1, min(limit, 200))

        stmt = select(AuditLog).order_by(desc(AuditLog.created_at))
        if username:
            stmt = stmt.where(AuditLog.username == username)
        if entity_type:
            stmt = stmt.where(AuditLog.entity_type == entity_type)
        if entity_id:
            stmt = stmt.where(AuditLog.entity_id == entity_id)
        if method:
            stmt = stmt.where(AuditLog.method == method)
        if status_min is not None:
            stmt = stmt.where(AuditLog.status_code >= int(status_min))
        if status_max is not None:
            stmt = stmt.where(AuditLog.status_code <= int(status_max))
        if start_date is not None:
            stmt = stmt.where(AuditLog.created_at >= start_date)
        if end_date is not None:
            stmt = stmt.where(AuditLog.created_at <= end_date)
        stmt = stmt.limit(limit)

        rows = (await ctx.db.execute(stmt)).scalars().all()
        items = [
            {
                "id": r.id,
                "organization_id": r.organization_id,
                "username": r.username,
                "method": r.method,
                "entity_type": r.entity_type,
                "entity_id": r.entity_id,
                "status_code": r.status_code,
                "duration_ms": r.duration_ms,
                "ip_address": r.ip_address,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]
        payload = {
            "count": len(items),
            "filters": {
                "username": username,
                "entity_type": entity_type,
                "entity_id": entity_id,
                "method": method,
                "status_min": status_min,
                "status_max": status_max,
                "start_date": kwargs.get("start_date"),
                "end_date": kwargs.get("end_date"),
                "limit": limit,
            },
            "audit_logs": items,
        }
        return ToolResult.ok(
            json.dumps(payload, ensure_ascii=False), count=len(items)
        )
