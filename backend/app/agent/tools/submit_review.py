"""submit_review tool — 送 entity 進入審核佇列(PENDING)。

對齊 POST /api/reviews。支援的 entity_type:
* ``testcase``(對 tree_nodes id;level_type 必須是 TESTCASE)
* ``defect``(對 defects id)
* ``document`` / ``script`` / ``report``(直接傳對應的 entity id 不另外驗存在)

requires_confirmation=true — 送審是流程動作,使用者要先確認對應 entity 真的
要進入審核狀態。
"""
from __future__ import annotations

import json
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select

from app.agent.tools.base import Tool, ToolContext, ToolResult
from app.auth.permissions_catalog import P
from app.auth.scope import ensure_project_in_scope
from app.auth.tenant import TenantQuery
from app.models.defect import Defect
from app.models.review import ReviewableEntityType
from app.models.tree_node import LevelType, TreeNode
from app.models.user import User
from app.services import review_service


class SubmitReviewTool(Tool):
    name = "submit_review"
    description = (
        "送 entity 進入審核佇列(狀態 → InReview)。"
        " entity_type 支援:testcase / defect / document / script / report。"
        " entity_id 是該業務 entity 的 UUID(testcase→tree_node.id,defect→defect.id)。"
        " assignee 可選 — 指派給某使用者審核(留空 = 任何具 review.manage 的人都可審)。"
        " 需要 review.submit 權限。requires_confirmation=true。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "entity_type": {
                "type": "string",
                "enum": ["testcase", "defect", "document", "script", "report"],
                "description": "entity 類別",
            },
            "entity_id": {"type": "string", "description": "業務 entity 的 UUID"},
            "assignee_username": {
                "type": "string",
                "description": "指派審核者的 username(留空 = 不指派)",
            },
        },
        "required": ["entity_type", "entity_id"],
        "additionalProperties": False,
    }
    casbin_permission = P.REVIEW_SUBMIT
    requires_confirmation = True

    async def execute(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        raw_type = (kwargs.get("entity_type") or "").strip().lower()
        entity_id = (kwargs.get("entity_id") or "").strip()
        assignee = (kwargs.get("assignee_username") or "").strip() or None

        if not (raw_type and entity_id):
            return ToolResult.fail(
                "missing_required",
                llm_visible="entity_type 與 entity_id 為必填。",
            )

        try:
            etype = ReviewableEntityType(raw_type)
        except ValueError:
            return ToolResult.fail(
                f"invalid_entity_type: {raw_type}",
                llm_visible=(
                    "entity_type 必須是 testcase / defect / document / script / report。"
                ),
            )

        # 對 testcase / defect 做存在性 + IDOR 驗證;document/script/report 因為
        # 沒有 org-scoped tenancy model,目前僅 superuser 可透過 tool 送審。
        if etype == ReviewableEntityType.TESTCASE:
            node = await ctx.db.get(TreeNode, entity_id)
            if node is None:
                return ToolResult.fail(
                    "testcase_not_found",
                    llm_visible=f"tree_node {entity_id} 不存在。",
                )
            if node.level_type != LevelType.TESTCASE:
                return ToolResult.fail(
                    "not_a_testcase_leaf",
                    llm_visible=(
                        f"node {entity_id} 不是 testcase 葉節點"
                        f"(level={node.level_type.value})。"
                    ),
                )
            try:
                await ensure_project_in_scope(
                    ctx.db,
                    node.project_id,
                    ctx.user,
                    not_found_detail="testcase 不在你可見範圍",
                )
            except HTTPException as e:
                return ToolResult.fail(
                    "out_of_scope",
                    llm_visible=str(e.detail) if e.detail else "testcase 不在你可見範圍。",
                )
        elif etype == ReviewableEntityType.DEFECT:
            defect = (
                await ctx.db.execute(
                    TenantQuery.for_(Defect).where(Defect.id == entity_id)
                )
            ).scalar_one_or_none()
            if defect is None:
                return ToolResult.fail(
                    "defect_not_found",
                    llm_visible=f"defect {entity_id} 不存在或非你所屬 org。",
                )
        else:
            # document / script / report 三類目前沒有 tenant scope model 可比對 — 為避免
            # 對任意 UUID 開啟 review record(造成跨 org 操作或 dangling 紀錄),
            # 僅 superuser 可送審這三類。
            if not ctx.user.is_superuser:
                return ToolResult.fail(
                    "entity_type_requires_superuser",
                    llm_visible=(
                        f"entity_type={etype.value} 目前僅 superuser 可透過 AI 送審;"
                        "請改走 UI 流程。"
                    ),
                )

        # 若指定 assignee — 對齊 router 的「assignee 必須真存在 + 同 org」防呆
        if assignee:
            target = (
                await ctx.db.execute(select(User).where(User.username == assignee))
            ).scalar_one_or_none()
            if target is None:
                return ToolResult.fail(
                    "assignee_not_found",
                    llm_visible=f"指派審核者 {assignee} 不存在。",
                )
            if (
                not ctx.user.is_superuser
                and target.organization_id is not None
                and ctx.organization_id is not None
                and target.organization_id != ctx.organization_id
            ):
                return ToolResult.fail(
                    "assignee_cross_org",
                    llm_visible="指派的審核者不在你的 organization。",
                )

        record = await review_service.submit(
            ctx.db,
            entity_type=etype,
            entity_id=entity_id,
            submitted_by=ctx.user.username,
            organization_id=ctx.organization_id,
            assignee=assignee,
            assignee_type="user" if assignee else None,
        )
        await ctx.db.commit()

        payload = {
            "status": "submitted",
            "review_record_id": record.id,
            "entity_type": etype.value,
            "entity_id": entity_id,
            "review_status": record.status.value,
            "assignee_username": assignee,
            "submitted_by": ctx.user.username,
        }
        return ToolResult.ok(json.dumps(payload, ensure_ascii=False))
