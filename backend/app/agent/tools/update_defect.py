"""update_defect tool — 改缺陷的 status / severity / priority / assignee / 內文。

對齊 PATCH /api/defects/{id}:狀態轉換走 ``_validate_transition``;傳 status=CLOSED
會 set closed_at;從 CLOSED 改回會清 closed_at(reopen)。
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select

from app.agent.tools.base import Tool, ToolContext, ToolResult
from app.auth.permissions_catalog import P
from app.auth.tenant import TenantQuery
from app.models.defect import (
    Defect,
    DefectPriority,
    DefectSeverity,
    DefectStatus,
)
from app.models.user import User
from app.routers.defects import _validate_transition  # 重用 router 內 transition table


class UpdateDefectTool(Tool):
    name = "update_defect"
    description = (
        "更新缺陷的任意欄位:status / severity / priority / assignee / 內文等。"
        " 狀態轉換受 RL 內建 transition table 限制(例如 Closed 不能直接跳 InProgress)。"
        " requires_confirmation=true。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "defect_id": {"type": "string", "description": "目標 defect UUID"},
            "title": {"type": "string", "maxLength": 300},
            "description": {"type": "string"},
            "steps_to_reproduce": {"type": "string"},
            "expected_result": {"type": "string"},
            "actual_result": {"type": "string"},
            "severity": {"type": "string", "enum": ["Critical", "Major", "Minor", "Trivial"]},
            "priority": {"type": "string", "enum": ["P0", "P1", "P2", "P3"]},
            "status": {
                "type": "string",
                "enum": [
                    "New", "Assigned", "InProgress", "InReview",
                    "ReworkRequired", "Verified", "Closed",
                ],
            },
            "assignee": {"type": "string", "maxLength": 100},
        },
        "required": ["defect_id"],
        "additionalProperties": False,
    }
    casbin_permission = P.DEFECT_WRITE
    requires_confirmation = True

    async def execute(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        defect_id = kwargs.get("defect_id")
        if not defect_id:
            return ToolResult.fail("missing_defect_id", llm_visible="defect_id 必填。")

        # 用 TenantQuery 確保 IDOR — 拿不到自家 org 的 defect
        defect = (
            await ctx.db.execute(
                TenantQuery.for_(Defect).where(Defect.id == defect_id)
            )
        ).scalar_one_or_none()
        if defect is None:
            return ToolResult.fail(
                "not_found", llm_visible=f"defect {defect_id} 不存在或非你所屬 org。"
            )

        # 收集待改欄位
        applied: dict[str, Any] = {}
        for key in (
            "title", "description", "steps_to_reproduce",
            "expected_result", "actual_result",
        ):
            v = kwargs.get(key)
            if v is not None:
                setattr(defect, key, v)
                applied[key] = "set"

        # assignee 必須真存在且同 org(對齊 submit_review 的 assignee 防呆 —
        # 避免 LLM 幻覺出 typo username 造成孤兒指派)
        if kwargs.get("assignee") is not None:
            new_assignee = (kwargs["assignee"] or "").strip()
            if new_assignee:
                target = (
                    await ctx.db.execute(
                        select(User).where(User.username == new_assignee)
                    )
                ).scalar_one_or_none()
                if target is None:
                    return ToolResult.fail(
                        "assignee_not_found",
                        llm_visible=f"指派對象 {new_assignee} 不存在,請先確認 username。",
                    )
                if (
                    not ctx.user.is_superuser
                    and target.organization_id is not None
                    and ctx.organization_id is not None
                    and target.organization_id != ctx.organization_id
                ):
                    return ToolResult.fail(
                        "assignee_cross_org",
                        llm_visible=f"指派對象 {new_assignee} 不在你的 organization。",
                    )
                defect.assignee = new_assignee
            else:
                defect.assignee = None  # 清空指派
            applied["assignee"] = "set"

        # Enum 欄位
        if kwargs.get("severity") is not None:
            try:
                defect.severity = DefectSeverity(kwargs["severity"])
                applied["severity"] = kwargs["severity"]
            except ValueError:
                return ToolResult.fail(
                    f"invalid_severity: {kwargs['severity']}",
                    llm_visible=f"severity 不合法:{kwargs['severity']}",
                )
        if kwargs.get("priority") is not None:
            try:
                defect.priority = DefectPriority(kwargs["priority"])
                applied["priority"] = kwargs["priority"]
            except ValueError:
                return ToolResult.fail(
                    f"invalid_priority: {kwargs['priority']}",
                    llm_visible=f"priority 不合法:{kwargs['priority']}",
                )

        # Status 走 transition 守門
        if kwargs.get("status") is not None:
            try:
                new_status = DefectStatus(kwargs["status"])
            except ValueError:
                return ToolResult.fail(
                    f"invalid_status: {kwargs['status']}",
                    llm_visible=f"status 不合法:{kwargs['status']}",
                )
            try:
                _validate_transition(defect.status, new_status)
            except HTTPException as e:
                return ToolResult.fail(
                    f"invalid_transition: {e.detail}",
                    llm_visible=f"狀態轉換被擋:{e.detail}(從 {defect.status.value} 不能直接到 {new_status.value})",
                )
            if new_status == DefectStatus.CLOSED and defect.closed_at is None:
                defect.closed_at = datetime.utcnow()
            if defect.status == DefectStatus.CLOSED and new_status != DefectStatus.CLOSED:
                defect.closed_at = None  # reopen
            defect.status = new_status
            applied["status"] = kwargs["status"]

        if not applied:
            return ToolResult.fail(
                "nothing_to_update",
                llm_visible="沒有提供任何要更新的欄位。",
            )

        defect.updated_at = datetime.utcnow()
        await ctx.db.commit()
        await ctx.db.refresh(defect)

        payload = {
            "status": "updated",
            "defect_id": defect.id,
            "code": defect.code,
            "applied_fields": list(applied.keys()),
            "defect_status": defect.status.value,
            "severity": defect.severity.value,
            "priority": defect.priority.value,
            "assignee": defect.assignee,
        }
        return ToolResult.ok(json.dumps(payload, ensure_ascii=False))
