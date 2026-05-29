"""remove_org_member tool — 把使用者從指定 organization 移除。

對齊 add_org_member 的鏡像版本。沒有對應的 router endpoint(已下架),
直接寫 OrgMembership 表的 status='inactive'(soft delete),保留歷史審計關聯。

紅線:
* requires_confirmation=True(極度 destructive — 失去 org 內所有資源可見度)
* 不能移除最後一個 admin user(系統會自我鎖定)
* 不能移除自己(避免自我鎖定)
* IDOR:跨 org 操作 fail-closed
"""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select, update

from app.agent.tools.base import Tool, ToolContext, ToolResult
from app.auth.permissions_catalog import P
from app.models.org_membership import OrgMembership
from app.models.organization import Organization
from app.models.user import User


class RemoveOrgMemberTool(Tool):
    name = "remove_org_member"
    description = (
        "把使用者從指定組織移除(將 OrgMembership.status 設為 inactive,保留歷史關聯)。"
        " **極度 destructive**:移除後該 user 失去此 org 所有資源的可見度。"
        " requires_confirmation=true。"
        " 不能移除自己;不能移除最後一個 admin。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "organization_id": {
                "type": "string",
                "description": "目標組織 UUID;留空 = 呼叫者目前所在 org",
            },
            "username": {
                "type": "string",
                "description": "目標使用者的 username",
            },
            "reason": {
                "type": "string",
                "description": "移除原因(會寫進 audit log;選填但強烈建議)",
            },
        },
        "required": ["username"],
        "additionalProperties": False,
    }
    casbin_permission = P.USER_MANAGE
    requires_confirmation = True

    async def execute(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        username = (kwargs.get("username") or "").strip()
        organization_id = (
            (kwargs.get("organization_id") or "").strip() or ctx.organization_id
        )

        if not username:
            return ToolResult.fail("missing_username", llm_visible="username 必填。")
        if not organization_id:
            return ToolResult.fail(
                "missing_organization_id",
                llm_visible="organization_id 必填(或呼叫者必須有 active org)。",
            )

        # IDOR:除 superuser 外,呼叫者 active org 必須等於目標 org
        if not ctx.user.is_superuser:
            if not ctx.organization_id or organization_id != ctx.organization_id:
                return ToolResult.fail(
                    "cross_org_forbidden",
                    llm_visible="不能從你不屬於的組織移除成員。",
                )

        # 不能移除自己
        if username == ctx.user.username:
            return ToolResult.fail(
                "cannot_remove_self",
                llm_visible="不能移除自己;要自己退出 org 請走 settings 頁面。",
            )

        org = await ctx.db.get(Organization, organization_id)
        if org is None:
            return ToolResult.fail(
                "organization_not_found",
                llm_visible=f"organization {organization_id} 不存在。",
            )

        # 找目標 OrgMembership
        om = (
            await ctx.db.execute(
                select(OrgMembership)
                .where(OrgMembership.username == username)
                .where(OrgMembership.organization_id == organization_id)
                .where(OrgMembership.status == "active")
            )
        ).scalar_one_or_none()
        if om is None:
            return ToolResult.fail(
                "not_a_member",
                llm_visible=f"{username} 不是 organization {organization_id} 的 active 成員。",
            )

        # 不能移除最後一個 admin — 取所有 admin role 的 active 成員,若只剩這一位就擋
        target_user = (
            await ctx.db.execute(select(User).where(User.username == username))
        ).scalar_one_or_none()
        if target_user is not None and target_user.is_superuser:
            other_admins = (
                await ctx.db.execute(
                    select(OrgMembership)
                    .where(OrgMembership.organization_id == organization_id)
                    .where(OrgMembership.status == "active")
                    .where(OrgMembership.username != username)
                )
            ).scalars().all()
            still_has_admin = False
            for other in other_admins:
                u = (
                    await ctx.db.execute(
                        select(User).where(User.username == other.username)
                    )
                ).scalar_one_or_none()
                if u is not None and u.is_superuser:
                    still_has_admin = True
                    break
            if not still_has_admin:
                return ToolResult.fail(
                    "last_admin",
                    llm_visible=(
                        f"{username} 是此 organization 唯一的 admin;不能移除"
                        "(會導致系統失去最高權限管理者)。"
                    ),
                )

        # Soft delete:status → inactive(保留歷史審計關聯)
        await ctx.db.execute(
            update(OrgMembership)
            .where(OrgMembership.id == om.id)
            .values(status="inactive", is_default=False)
        )

        # Casbin re-sync
        try:
            from app.auth.casbin_sync import schedule_user_resync

            schedule_user_resync(username)
        except Exception:  # noqa: BLE001
            pass

        await ctx.db.commit()
        payload = {
            "status": "removed",
            "organization_id": organization_id,
            "organization_slug": org.slug,
            "username": username,
            "reason": kwargs.get("reason") or None,
            "soft_delete": True,
        }
        return ToolResult.ok(json.dumps(payload, ensure_ascii=False))
