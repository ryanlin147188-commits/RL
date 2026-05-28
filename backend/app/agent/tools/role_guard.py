"""共用的 role 指派防護 — 阻止透過 add_*_member / assign_project_role
這類 tool 把高權限 role 指給呼叫者(privilege escalation)。

判定條件:
* superuser 一律放行。
* role 的 ``permissions_json`` 若含任何 ``*.manage`` 權限,非 superuser 拒絕。
  ``*.manage`` 在這個專案的權限模型代表「能管別人」(user.manage / role.manage /
  org.manage 等),把這類 role 指給任意成員等於把組織管理權交出去 — 必須由
  superuser 主動操作。
"""
from __future__ import annotations

from typing import Optional

from app.agent.tools.base import ToolContext, ToolResult
from app.models.role import Role


def _is_privileged_role(role: Role) -> bool:
    perms = getattr(role, "permissions_json", None) or []
    for key in perms:
        if isinstance(key, str) and key.endswith(".manage"):
            return True
    return False


async def ensure_role_assignable(
    ctx: ToolContext, role: Role
) -> Optional[ToolResult]:
    """若 role 屬於管理級別,只允許 superuser 指派。回 None = 放行。"""
    if ctx.user.is_superuser:
        return None
    if _is_privileged_role(role):
        return ToolResult.fail(
            "role_escalation_forbidden",
            llm_visible=(
                f"role {role.id}({role.name})含 *.manage 權限,"
                "僅 superuser 可指派;請改用較低權限的 role。"
            ),
        )
    return None
