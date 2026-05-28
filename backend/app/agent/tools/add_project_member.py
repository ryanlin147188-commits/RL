"""add_project_member tool — 把某 user 加入指定 project(成為 ProjectMember)。

對齊 POST /api/projects/{project_id}/members(router 既有實作)。
* 該 user 必須先是該 project 所屬 org 的 OrgMembership(active)— 跨 org 擋。
* (project_id, username) 已存在 → 409 conflict。
* role_id 留空 = 從 OrgMembership.role_id 繼承。
* requires_confirmation=true(影響使用者在該 project 內的可見/可改範圍)。
"""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select

from app.agent.tools.base import Tool, ToolContext, ToolResult
from app.agent.tools.role_guard import ensure_role_assignable
from app.auth.permissions_catalog import P
from app.models.org_membership import OrgMembership
from app.models.project import Project
from app.models.project_member import ProjectMember
from app.models.role import Role
from app.models.user import User


class AddProjectMemberTool(Tool):
    name = "add_project_member"
    description = (
        "把使用者加入指定專案(建立 ProjectMember)。"
        " 前提:該使用者必須先是該 project 所屬 org 的成員(OrgMembership active)。"
        " role_id 留空 = 從 OrgMembership.role_id 繼承該 user 在此專案的角色。"
        " 需要 user.manage 權限。requires_confirmation=true。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "project_id": {"type": "string", "description": "目標專案 UUID"},
            "username": {"type": "string", "description": "目標使用者的 username"},
            "role_id": {
                "type": "string",
                "description": "在此 project 內的 role UUID;留空 = 從 OrgMembership 繼承",
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
        role_id = (kwargs.get("role_id") or "").strip() or None

        if not (project_id and username):
            return ToolResult.fail(
                "missing_required",
                llm_visible="project_id 與 username 為必填。",
            )

        # 驗 project 存在 + IDOR(fail-closed:只要 organization_id 對不上就拒絕,
        # 不再因 proj.organization_id is None / ctx.organization_id is None 短路)
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

        # User PK 是 UUID — 必須用 select 查 username
        target = (
            await ctx.db.execute(select(User).where(User.username == username))
        ).scalar_one_or_none()
        if target is None:
            return ToolResult.fail(
                "user_not_found",
                llm_visible=f"找不到使用者 {username}。",
            )

        # 必要前提:該 user 必須是該 project 所屬 org 的 active OrgMembership
        # (superuser 例外 — 對齊 router)
        om = (
            await ctx.db.execute(
                select(OrgMembership)
                .where(OrgMembership.username == username)
                .where(OrgMembership.organization_id == proj.organization_id)
                .where(OrgMembership.status == "active")
            )
        ).scalar_one_or_none()
        if om is None and not target.is_superuser:
            return ToolResult.fail(
                "not_in_org",
                llm_visible=(
                    f"{username} 不是此 project 所屬 organization 的成員;"
                    " 請先用 add_org_member 加進 org,再加成員到 project。"
                ),
            )

        if role_id:
            role = await ctx.db.get(Role, role_id)
            if role is None:
                return ToolResult.fail(
                    f"invalid_role_id: {role_id}",
                    llm_visible=f"role {role_id} 不存在。",
                )
            guard_err = await ensure_role_assignable(ctx, role)
            if guard_err is not None:
                return guard_err

        # 重複加 → 409
        existing = (
            await ctx.db.execute(
                select(ProjectMember)
                .where(ProjectMember.project_id == project_id)
                .where(ProjectMember.username == username)
            )
        ).scalar_one_or_none()
        if existing is not None:
            return ToolResult.fail(
                "already_member",
                llm_visible=(
                    f"{username} 已是 project {project_id} 的成員"
                    f"(status={existing.status})。"
                ),
            )

        pm = ProjectMember(
            project_id=project_id,
            username=username,
            role_id=role_id,
            status="active",
            invited_by=ctx.user.username,
        )
        ctx.db.add(pm)
        await ctx.db.flush()

        try:
            from app.auth.casbin_sync import schedule_user_resync
            schedule_user_resync(username)
        except Exception:  # noqa: BLE001
            pass

        await ctx.db.commit()
        payload = {
            "status": "added",
            "membership_id": pm.id,
            "project_id": project_id,
            "username": username,
            "role_id": role_id,
            "inherits_org_role": role_id is None,
        }
        return ToolResult.ok(json.dumps(payload, ensure_ascii=False))
