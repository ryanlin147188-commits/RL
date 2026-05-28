"""add_org_member tool — 把既有 user 加進指定 organization。

RL 已下架 ``POST /orgs/{id}/members``(舊 router 與「邀請碼/email-domain 自動歸屬」
一起拿掉)。LLM 想新增 org member 沒現成 endpoint 可用 → 由 tool 直接寫
``org_memberships`` 表;對齊 ``app/auth/personal_org.py`` 與 ``admin_create_user``
的 OrgMembership 建立邏輯。

* 需要 user.manage 權限(組織管理員等級)
* 同 (username, org_id) 已存在 → 409 conflict
* role_id 留空 = OrgMembership.role_id NULL → 該 user 在此 org 走 system default
* status 預設 active(沒 email 邀請流程可走);若 set_default=True 會把該
  user 其他 OrgMembership 的 is_default flip 為 False(只能有一個 default)
* requires_confirmation=True(影響該 user 進入 org 的可見性)
"""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select, update

from app.agent.tools.base import Tool, ToolContext, ToolResult
from app.agent.tools.role_guard import ensure_role_assignable
from app.auth.permissions_catalog import P
from app.models.org_membership import OrgMembership
from app.models.organization import Organization
from app.models.role import Role
from app.models.user import User


class AddOrgMemberTool(Tool):
    name = "add_org_member"
    description = (
        "把既有使用者加入指定組織(建立 OrgMembership)。"
        " username 必須對應已存在的 user;role_id 留空 = 該 user 在此 org"
        " 走系統預設角色。set_default=true 會把這筆設為該 user 的 active org"
        "(同步把其他 OrgMembership.is_default 改 False)。"
        " 需要 user.manage 權限。requires_confirmation=true。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "organization_id": {
                "type": "string",
                "description": "目標組織 UUID;留空 = 加進呼叫者目前所在 org",
            },
            "username": {"type": "string", "description": "目標使用者的 username"},
            "role_id": {
                "type": "string",
                "description": "在此 org 內的 role UUID;留空 = 繼承系統預設",
            },
            "set_default": {
                "type": "boolean",
                "description": "是否設成該 user 的預設 active org(預設 false)",
            },
        },
        "required": ["username"],
        "additionalProperties": False,
    }
    casbin_permission = P.USER_MANAGE
    requires_confirmation = True

    async def execute(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        username = (kwargs.get("username") or "").strip()
        role_id = (kwargs.get("role_id") or "").strip() or None
        set_default = bool(kwargs.get("set_default") or False)
        organization_id = (kwargs.get("organization_id") or "").strip() or ctx.organization_id

        if not username:
            return ToolResult.fail("missing_username", llm_visible="username 必填。")
        if not organization_id:
            return ToolResult.fail(
                "missing_organization_id",
                llm_visible="organization_id 必填(或呼叫者必須有 active org)。",
            )

        # IDOR 防護:除 superuser 外,呼叫者必須有 active org 且要等於目標 org
        # (fail-closed:ctx.organization_id is None 時直接拒絕,不再短路放行)
        if not ctx.user.is_superuser:
            if not ctx.organization_id or organization_id != ctx.organization_id:
                return ToolResult.fail(
                    "cross_org_forbidden",
                    llm_visible="不能把使用者加進你不屬於的組織。",
                )

        org = await ctx.db.get(Organization, organization_id)
        if org is None:
            return ToolResult.fail(
                "organization_not_found",
                llm_visible=f"organization {organization_id} 不存在。",
            )

        # User PK 是 UUID (id),不是 username — 必須用 select 查
        target = (
            await ctx.db.execute(select(User).where(User.username == username))
        ).scalar_one_or_none()
        if target is None:
            return ToolResult.fail(
                "user_not_found",
                llm_visible=f"找不到使用者 {username}(請先建立帳號)。",
            )

        if role_id:
            role = await ctx.db.get(Role, role_id)
            if role is None:
                return ToolResult.fail(
                    f"invalid_role_id: {role_id}",
                    llm_visible=f"role {role_id} 不存在。",
                )
            # 防止權限提升:任何含 *.manage 的 role 僅 superuser 可指派
            guard_err = await ensure_role_assignable(ctx, role)
            if guard_err is not None:
                return guard_err

        # 已存在 → 409
        existing = (
            await ctx.db.execute(
                select(OrgMembership)
                .where(OrgMembership.username == username)
                .where(OrgMembership.organization_id == organization_id)
            )
        ).scalar_one_or_none()
        if existing is not None:
            return ToolResult.fail(
                "already_member",
                llm_visible=(
                    f"{username} 已是 organization {organization_id} 的成員"
                    f"(status={existing.status})。"
                ),
            )

        # 若 set_default=True,先把該 user 其他 OrgMembership.is_default 全清
        if set_default:
            await ctx.db.execute(
                update(OrgMembership)
                .where(OrgMembership.username == username)
                .where(OrgMembership.is_default.is_(True))
                .values(is_default=False)
            )

        om = OrgMembership(
            username=username,
            organization_id=organization_id,
            role_id=role_id,
            is_default=set_default,
            status="active",
            invited_by=ctx.user.username,
        )
        ctx.db.add(om)
        await ctx.db.flush()

        # 觸發 Casbin re-sync 讓新權限立刻生效
        try:
            from app.auth.casbin_sync import schedule_user_resync
            schedule_user_resync(username)
        except Exception:  # noqa: BLE001
            pass

        await ctx.db.commit()
        payload = {
            "status": "added",
            "membership_id": om.id,
            "organization_id": organization_id,
            "organization_slug": org.slug,
            "username": username,
            "role_id": role_id,
            "is_default": set_default,
        }
        return ToolResult.ok(json.dumps(payload, ensure_ascii=False))
