"""remove_project_member tool — 把使用者從指定 project 移除。

對齊 DELETE /api/projects/{project_id}/members/{username}。
保留 OrgMembership,只從這個 project 移除(該 user 仍能看到 org 內其他 project)。

紅線:
* requires_confirmation=True
* 不能移除自己(對齊 router — 自己退出走 /leave 端點)
* IDOR:跨 org 操作 fail-closed
"""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select

from app.agent.tools.base import Tool, ToolContext, ToolResult
from app.auth.permissions_catalog import P
from app.models.project import Project
from app.models.project_member import ProjectMember


class RemoveProjectMemberTool(Tool):
    name = "remove_project_member"
    description = (
        "把使用者從指定 project 移除(刪除 ProjectMember)。"
        " 該 user 仍保留 OrgMembership,只是看不到此專案。"
        " **destructive**:該 user 失去 project 內所有資源(testcase/defect/report)的可見度。"
        " requires_confirmation=true。不能移除自己(自己退出請走 settings)。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "project_id": {"type": "string", "description": "目標 project UUID"},
            "username": {"type": "string", "description": "目標 user 的 username"},
            "reason": {
                "type": "string",
                "description": "移除原因(會寫進 audit log;選填但強烈建議)",
            },
        },
        "required": ["project_id", "username"],
        "additionalProperties": False,
    }
    casbin_permission = P.USER_MANAGE
    requires_confirmation = True

    async def execute(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        project_id = (kwargs.get("project_id") or "").strip()
        username = (kwargs.get("username") or "").strip()

        if not (project_id and username):
            return ToolResult.fail(
                "missing_required",
                llm_visible="project_id 與 username 為必填。",
            )

        # 不能移除自己(對齊 router 行為)
        if username == ctx.user.username and not ctx.user.is_superuser:
            return ToolResult.fail(
                "cannot_remove_self",
                llm_visible=(
                    "不能移除自己;要自己退出 project 請走 settings → 退出專案。"
                ),
            )

        # 驗 project 存在 + IDOR(fail-closed)
        proj = await ctx.db.get(Project, project_id)
        if proj is None:
            return ToolResult.fail(
                "project_not_found",
                llm_visible=f"project {project_id} 不存在。",
            )
        if not ctx.user.is_superuser:
            if (
                not proj.organization_id
                or not ctx.organization_id
                or proj.organization_id != ctx.organization_id
            ):
                return ToolResult.fail(
                    "project_not_found",
                    llm_visible=f"project {project_id} 不存在。",
                )

        # 找 ProjectMember
        pm = (
            await ctx.db.execute(
                select(ProjectMember)
                .where(ProjectMember.project_id == project_id)
                .where(ProjectMember.username == username)
            )
        ).scalar_one_or_none()
        if pm is None:
            return ToolResult.fail(
                "not_a_member",
                llm_visible=f"{username} 不是 project {project_id} 的成員。",
            )

        membership_id = pm.id
        await ctx.db.delete(pm)
        await ctx.db.flush()

        try:
            from app.auth.casbin_sync import schedule_user_resync

            schedule_user_resync(username)
        except Exception:  # noqa: BLE001
            pass

        await ctx.db.commit()
        payload = {
            "status": "removed",
            "project_id": project_id,
            "project_name": proj.name,
            "username": username,
            "removed_membership_id": membership_id,
            "reason": kwargs.get("reason") or None,
        }
        return ToolResult.ok(json.dumps(payload, ensure_ascii=False))
