"""delete_defect tool — 刪除一筆缺陷。

對齊 DELETE /api/defects/{id}。requires_confirmation=true(資料不可復原)。
"""
from __future__ import annotations

import json
from typing import Any

from app.agent.tools.base import Tool, ToolContext, ToolResult
from app.auth.permissions_catalog import P
from app.auth.tenant import TenantQuery
from app.models.defect import Defect
from app.services import defect_service


class DeleteDefectTool(Tool):
    name = "delete_defect"
    description = (
        "永久刪除一筆缺陷紀錄。**不可復原** — Tenant 防 IDOR(別 org 的 defect 拿不到)。"
        " requires_confirmation=true。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "defect_id": {"type": "string", "description": "目標 defect UUID"},
        },
        "required": ["defect_id"],
        "additionalProperties": False,
    }
    casbin_permission = P.DEFECT_DELETE
    requires_confirmation = True

    async def execute(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        defect_id = kwargs.get("defect_id")
        if not defect_id:
            return ToolResult.fail("missing_defect_id", llm_visible="defect_id 必填。")

        defect = (
            await ctx.db.execute(
                TenantQuery.for_(Defect).where(Defect.id == defect_id)
            )
        ).scalar_one_or_none()
        if defect is None:
            return ToolResult.fail(
                "not_found",
                llm_visible=f"defect {defect_id} 不存在或非你所屬 org。",
            )

        code = defect.code
        title = defect.title
        # 用 service.hard_delete 一併清掉指向此 defect 的 review_records,
        # 避免 dangling review record 造成前端 500
        await defect_service.hard_delete(ctx.db, defect)
        await ctx.db.commit()

        payload = {
            "status": "deleted",
            "defect_id": defect_id,
            "code": code,
            "title": title,
        }
        return ToolResult.ok(json.dumps(payload, ensure_ascii=False))
