"""assign_project_role tool — 把某 user 在某 project 的 role 改成指定 role_id。

對齊 PUT /api/projects/{id}/members(batch)但簡化成單一 user。
* role_id=null 視為「繼承 org 預設」
* 需要 user.manage 權限(組織管理員等級)
* requires_confirmation=true(影響該 user 在 project 內的可見/可改範圍)
"""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select

from app.agent.tools.base import Tool, ToolContext, ToolResult
from app.auth.permissions_catalog import P
from app.models.project import Project
from app.models.project_member import ProjectMember
from app.models.role import Role


class AssignProjectRoleTool(Tool):
    name = "assign_project_role"
    description = (
        "更新指定使用者在指定專案的角色(role_id)。"
        " role_id 留空 = 繼承組織預設角色。"
        " 需要 user.manage 權限(組織管理員等級)。requires_confirmation=true。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "project_id": {"type": "string", "description": "目標 project UUID"},
            "username": {"type": "string", "description": "目標使用者的 username"},
            "role_id": {
                "type": "string",
                "description": "新 role UUID;留空 = 繼承 org 預設(設成 null)",
            },
        },
        "required": ["project_id", "username"],
        "additionalProperties": False,
    }
    casbin_permission = P.USER_MANAGE
    requires_confirmation = True

    async def execute(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        project_id = kwargs.get("project_id")
        username = (kwargs.get("username") or "").strip()
        role_id = (kwargs.get("role_id") or "").strip() or None

        if not (project_id and username):
            return ToolResult.fail(
                "missing_required",
                llm_visible="project_id 與 username 為必填。",
            )

        # 驗 project 存在且屬該 org(IDOR 防護)
        proj = await ctx.db.get(Project, project_id)
        if proj is None or (proj.organization_id and proj.organization_id != ctx.organization_id):
            if not ctx.user.is_superuser:
                return ToolResult.fail(
                    "project_not_found",
                    llm_visible=f"project {project_id} 不存在或非你所屬 org。",
                )

        # 驗 role 存在(若有指定)
        if role_id:
            role = await ctx.db.get(Role, role_id)
            if role is None:
                return ToolResult.fail(
                    f"invalid_role_id: {role_id}",
                    llm_visible=f"role {role_id} 不存在。",
                )

        pm = (await ctx.db.execute(
            select(ProjectMember)
            .where(ProjectMember.project_id == project_id)
            .where(ProjectMember.username == username)
        )).scalar_one_or_none()
        if pm is None:
            return ToolResult.fail(
                "not_a_member",
                llm_visible=(
                    f"使用者 {username} 還不是 project {project_id} 的成員。"
                    "請先讓使用者加入該 project(或改用 add_project_member tool —目前未實作)。"
                ),
            )

        old_role = pm.role_id
        pm.role_id = role_id
        await ctx.db.flush()

        # 觸發 Casbin re-sync 讓新權限立刻生效(對齊 router 邏輯)
        try:
            from app.auth.casbin_sync import schedule_user_resync
            schedule_user_resync(username)
        except Exception:  # noqa: BLE001
            pass  # 同步失敗不擋 tool

        await ctx.db.commit()
        payload = {
            "status": "updated",
            "project_id": project_id,
            "username": username,
            "old_role_id": old_role,
            "new_role_id": role_id,
            "inherits_org_default": role_id is None,
        }
        return ToolResult.ok(json.dumps(payload, ensure_ascii=False))
