"""resolve_review tool — 對既有的 review_record 執行 approve / reject / revert。

對齊三支 router endpoint:
* POST /api/reviews/{id}/approve
* POST /api/reviews/{id}/reject(reason 必填)
* POST /api/reviews/{id}/revert(reason 必填;approved/rejected → PENDING)

權限分層:
* approve / reject:review.manage(送審者本人不可自審 — 對齊 router)
* revert:Admin role 或 review.manage(對齊 router 的 _ensure_admin)

requires_confirmation=true — 三個動作都會改變 entity 的審核狀態,影響後續編輯
鎖定(approved 後 entity 變唯讀)。
"""
from __future__ import annotations

import json
from typing import Any

from fastapi import HTTPException

from app.agent.tools.base import Tool, ToolContext, ToolResult
from app.auth.permissions_catalog import P
from app.auth.tenant import TenantQuery
from app.models.review import ReviewRecord
from app.models.role import Role
from app.services import review_service


class ResolveReviewTool(Tool):
    name = "resolve_review"
    description = (
        "對 review_record 執行 approve(通過) / reject(退回,需 reason)"
        " / revert(把已通過或退回的解鎖,需 reason)。"
        " 送審者本人不能 approve/reject 自己送的(自審防呆)。"
        " revert 通常只給 Admin / 有 review.manage 權限的人。"
        " 需要 review.manage 權限。requires_confirmation=true。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "review_record_id": {
                "type": "string",
                "description": "review_records.id(UUID)",
            },
            "action": {
                "type": "string",
                "enum": ["approve", "reject", "revert"],
                "description": "要執行的動作",
            },
            "reason": {
                "type": "string",
                "description": "reject / revert 必填(approve 忽略)",
            },
        },
        "required": ["review_record_id", "action"],
        "additionalProperties": False,
    }
    casbin_permission = P.REVIEW_MANAGE
    requires_confirmation = True

    async def execute(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        record_id = (kwargs.get("review_record_id") or "").strip()
        action = (kwargs.get("action") or "").strip().lower()
        reason = (kwargs.get("reason") or "").strip() or None

        if not (record_id and action):
            return ToolResult.fail(
                "missing_required",
                llm_visible="review_record_id 與 action 為必填。",
            )
        if action not in ("approve", "reject", "revert"):
            return ToolResult.fail(
                f"invalid_action: {action}",
                llm_visible="action 必須是 approve / reject / revert。",
            )
        if action in ("reject", "revert") and not reason:
            return ToolResult.fail(
                "missing_reason",
                llm_visible=f"{action} 必填 reason(說明退回 / 解鎖原因)。",
            )

        # 載入 record(TenantQuery 套 org 過濾,跨 org 拿不到)
        record = (
            await ctx.db.execute(
                TenantQuery.for_(ReviewRecord).where(ReviewRecord.id == record_id)
            )
        ).scalar_one_or_none()
        if record is None:
            return ToolResult.fail(
                "not_found",
                llm_visible=f"review_record {record_id} 不存在或非你所屬 org。",
            )

        # 對齊 router 自審防呆(approve / reject 用)
        if action in ("approve", "reject"):
            if (
                not ctx.user.is_superuser
                and record.submitted_by == ctx.user.username
            ):
                return ToolResult.fail(
                    "self_review_forbidden",
                    llm_visible="送審者本人不可審核此筆紀錄。",
                )

        # revert 對齊 _ensure_admin:superuser、Admin role、或具 review.manage
        if action == "revert":
            allowed = ctx.user.is_superuser
            if not allowed and ctx.user.role_id:
                role = await ctx.db.get(Role, ctx.user.role_id)
                if role is not None:
                    if role.name == "Admin":
                        allowed = True
                    elif "review.manage" in (role.permissions_json or []):
                        allowed = True
            if not allowed:
                return ToolResult.fail(
                    "permission_denied",
                    llm_visible="revert 需要 Admin role 或 review.manage 權限。",
                )

        try:
            if action == "approve":
                record = await review_service.approve(
                    ctx.db, record=record, reviewer=ctx.user.username
                )
            elif action == "reject":
                record = await review_service.reject(
                    ctx.db, record=record, reviewer=ctx.user.username, reason=reason or ""
                )
            else:  # revert
                record = await review_service.revert(
                    ctx.db, record=record, actor=ctx.user.username, reason=reason or ""
                )
        except HTTPException as e:
            return ToolResult.fail(
                f"action_failed: {e.detail}",
                llm_visible=f"{action} 失敗:{e.detail}",
            )

        await ctx.db.commit()
        payload = {
            "status": action + "d" if action.endswith("e") else action + "ed",
            "review_record_id": record.id,
            "entity_type": record.entity_type.value,
            "entity_id": record.entity_id,
            "new_status": record.status.value,
            "actor": ctx.user.username,
            "reason": reason,
        }
        return ToolResult.ok(json.dumps(payload, ensure_ascii=False))
