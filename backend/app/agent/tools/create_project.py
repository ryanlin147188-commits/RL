"""create_project tool — 建一個測試專案。

跟既有 POST /api/projects 同樣邏輯:寫 Project + 自動加 ProjectMember 給
建立者 + 同 org user 自動加入。``requires_confirmation=True``(寫 DB 且
影響整 org 看得到的專案列表)。
"""
from __future__ import annotations

import json
import logging
from typing import Any

from app.agent.tools.base import Tool, ToolContext, ToolResult
from app.auth.permissions_catalog import P
from app.models.project import Project
from app.models.project_member import ProjectMember

log = logging.getLogger(__name__)


class CreateProjectTool(Tool):
    name = "create_project"
    description = (
        "在 RL 平台上建立一個新測試專案。**這是 destructive 動作**:會寫入 DB,"
        " 同組織所有成員會自動加入該專案。requires_confirmation=true,"
        " 使用者必須在 UI 按下「同意」才會真實建立。"
        " 預設 status 為 InProgress。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "maxLength": 200, "description": "專案名稱(必填)"},
            "description": {"type": "string", "description": "專案描述"},
            "owner": {"type": "string", "description": "專案負責人 username(可選)"},
            "status": {
                "type": "string",
                "enum": ["New", "Assigned", "InProgress", "InReview", "ReworkRequired", "Verified", "Closed"],
                "description": "專案狀態;預設 InProgress",
            },
            "start_date": {"type": "string", "description": "開始日 YYYY-MM-DD(可選)"},
            "target_date": {"type": "string", "description": "目標完成日 YYYY-MM-DD(可選)"},
            "tags": {"type": "string", "description": "標籤,逗號分隔字串"},
        },
        "required": ["name"],
        "additionalProperties": False,
    }
    casbin_permission = P.PROJECT_WRITE
    requires_confirmation = True

    async def execute(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        name = (kwargs.get("name") or "").strip()
        if not name:
            return ToolResult.fail("missing_name", llm_visible="name 為必填欄位。")

        # 沿用 projects.py 內 _normalize_project_status 的「未指定 → InProgress」
        status = (kwargs.get("status") or "InProgress").strip()
        project = Project(
            name=name,
            organization_id=ctx.organization_id,
            description=kwargs.get("description") or None,
            owner=kwargs.get("owner") or None,
            status=status,
            start_date=kwargs.get("start_date") or None,
            target_date=kwargs.get("target_date") or None,
            tags=kwargs.get("tags") or None,
        )
        ctx.db.add(project)
        await ctx.db.flush()
        # 建立者自動成為 member(沿用 router 邏輯)
        ctx.db.add(ProjectMember(
            project_id=project.id,
            username=ctx.user.username,
            role_id=None,
            status="active",
        ))
        await ctx.db.flush()
        # 同 org 所有 active user 自動加入(沿用 router 邏輯)
        try:
            from app.auth.project_membership import ensure_project_has_all_org_users
            await ensure_project_has_all_org_users(ctx.db, project)
        except Exception:  # noqa: BLE001 - 同 org 自動加成員失敗不阻擋
            log.exception("ensure_project_has_all_org_users failed for new project %s", project.id)

        await ctx.db.commit()
        await ctx.db.refresh(project)

        payload = {
            "status": "created",
            "project_id": project.id,
            "name": project.name,
            "owner": project.owner,
            "project_status": project.status,
            "view_url": f"/#/projects/{project.id}",
            "next_step": (
                "若要繼續建立測試樹結構(Feature → Platform → Page → Scenario → Testcase),"
                " 用 create_tree_node tool 一層一層加;parent_id 留空 = 建 Feature 根層。"
            ),
        }
        return ToolResult.ok(
            json.dumps(payload, ensure_ascii=False),
            project_id=project.id,
        )
