"""create_defect tool — 寫一條缺陷。

Destructive 動作(寫 DB,雖然不長時也不占容器):
* ``requires_confirmation = True`` — 跳二次確認 modal,user 看完欄位內容才能送
* ``casbin_permission = P.DEFECT_WRITE`` — 對齊語意(router 為歷史原因用
  TESTCASE_WRITE,但 agent tool 應該以正確的權限分類)

重用既有 ``_next_code()`` private helper 生 DEF-NNNNN 編號;邏輯不複製避免
兩處走偏(如果哪天 _next_code 改成 DB sequence,這邊也自動跟著走)。
"""
from __future__ import annotations

import json
import uuid
from typing import Any

from app.agent.tools.base import Tool, ToolContext, ToolResult
from app.auth.permissions_catalog import P
from app.models.defect import (
    Defect,
    DefectPriority,
    DefectSeverity,
    DefectStatus,
)
from app.routers.defects import _next_code  # 重用既有編號生成


class CreateDefectTool(Tool):
    name = "create_defect"
    description = (
        "建立一筆新的缺陷紀錄。**這是 destructive 動作**,會寫入 DB;"
        " 使用者必須在 UI 按下「同意」才會真實執行。"
        " 必填 project_id 與 title;其餘欄位可選。"
        " severity 預設 Minor、priority 預設 P2。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "project_id": {
                "type": "string",
                "description": "目標專案 ID(UUID)",
            },
            "title": {
                "type": "string",
                "maxLength": 300,
                "description": "缺陷標題(必填)",
            },
            "description": {"type": "string", "description": "缺陷描述"},
            "steps_to_reproduce": {
                "type": "string",
                "description": "重現步驟(可多行)",
            },
            "expected_result": {"type": "string", "description": "預期結果"},
            "actual_result": {"type": "string", "description": "實際結果"},
            "severity": {
                "type": "string",
                "enum": ["Critical", "Major", "Minor", "Trivial"],
            },
            "priority": {"type": "string", "enum": ["P0", "P1", "P2", "P3"]},
            "assignee": {"type": "string", "description": "指派對象 username"},
            "linked_testcase_id": {
                "type": "string",
                "description": "關聯 testcase node ID",
            },
            "linked_report_id": {
                "type": "string",
                "description": "關聯 execution_report ID",
            },
        },
        "required": ["project_id", "title"],
        "additionalProperties": False,
    }
    casbin_permission = P.DEFECT_WRITE
    requires_confirmation = True

    async def execute(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        project_id = kwargs.get("project_id")
        title = (kwargs.get("title") or "").strip()
        if not project_id or not title:
            return ToolResult.fail(
                "missing_required",
                llm_visible="project_id 與 title 為必填欄位。",
            )

        severity_str = kwargs.get("severity") or "Minor"
        priority_str = kwargs.get("priority") or "P2"
        try:
            severity = DefectSeverity(severity_str)
            priority = DefectPriority(priority_str)
        except ValueError as e:
            return ToolResult.fail(
                f"invalid_enum: {e}",
                llm_visible=f"severity / priority 值不合法:{e}",
            )

        try:
            code = await _next_code(ctx.db, project_id)
        except Exception as e:  # noqa: BLE001
            return ToolResult.fail(
                f"next_code_failed: {type(e).__name__}",
                llm_visible=f"無法產生缺陷編號(專案 ID 可能無效):{e}",
            )

        assignee = kwargs.get("assignee")
        defect = Defect(
            id=str(uuid.uuid4()),
            project_id=project_id,
            code=code,
            title=title,
            description=kwargs.get("description") or None,
            steps_to_reproduce=kwargs.get("steps_to_reproduce") or None,
            expected_result=kwargs.get("expected_result") or None,
            actual_result=kwargs.get("actual_result") or None,
            severity=severity,
            priority=priority,
            status=DefectStatus.ASSIGNED if assignee else DefectStatus.NEW,
            reporter=ctx.user.username,
            assignee=assignee or None,
            linked_testcase_id=kwargs.get("linked_testcase_id") or None,
            linked_report_id=kwargs.get("linked_report_id") or None,
            attachments_json=[],
        )
        ctx.db.add(defect)
        # commit 讓其他 read 看得到(沿用 router 的 commit pattern)
        await ctx.db.commit()
        await ctx.db.refresh(defect)

        payload = {
            "status": "created",
            "defect_id": defect.id,
            "code": defect.code,
            "project_id": defect.project_id,
            "title": defect.title,
            "severity": defect.severity.value,
            "priority": defect.priority.value,
            "defect_status": defect.status.value,
            "assignee": defect.assignee,
            "view_url": f"/#/defects/{defect.id}",
        }
        return ToolResult.ok(
            json.dumps(payload, ensure_ascii=False),
            defect_id=defect.id,
            code=defect.code,
        )
