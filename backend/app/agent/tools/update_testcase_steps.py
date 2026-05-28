"""update_testcase_steps tool — 寫測試案例的步驟內容(Robot syntax)。

對齊 PUT /api/testcases/{node_id}:
* ``ac_text`` — 驗收條件文字
* ``setup_text`` — 前置條件文字
* ``steps_json`` — 步驟陣列(Robot keyword + 參數結構;前端表格轉成的 JSON)

讓 LLM 真正能「從頭到尾寫完整測試案例」— 跟 create_tree_node(建 testcase 節點)
配對使用。**這是 destructive 動作**,requires_confirmation=true;若該案例已經
被 reviewer 核准(review approved),會被 review_service 鎖,raise 400。
"""
from __future__ import annotations

import json
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select

from app.agent.tools.base import Tool, ToolContext, ToolResult
from app.auth.permissions_catalog import P
from app.auth.scope import ensure_project_writable
from app.models.review import ReviewableEntityType
from app.models.testcase_content import TestcaseContent
from app.models.tree_node import LevelType, TreeNode
from app.services import review_service


class UpdateTestcaseStepsTool(Tool):
    name = "update_testcase_steps"
    description = (
        "更新測試案例的步驟內容(Robot syntax)。傳的欄位才會更新,沒傳的保留。"
        " 必須指向一個 level_type=TESTCASE 的 tree node;"
        "若該案例已被審核 approved,會被拒絕(請先 revert)。"
        " steps_json 是陣列,每筆 {keyword, args} 結構(對齊既有前端表格)。"
        " requires_confirmation=true。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "node_id": {"type": "string", "description": "目標 testcase 葉節點 ID"},
            "ac_text": {"type": "string", "description": "驗收條件文字"},
            "setup_text": {"type": "string", "description": "前置條件文字"},
            "steps_json": {
                "type": "array",
                "items": {"type": "object"},
                "description": "步驟陣列,例 [{keyword:\"Click Element\",args:[\"id=login\"]}, ...]",
            },
        },
        "required": ["node_id"],
        "additionalProperties": False,
    }
    casbin_permission = P.TESTCASE_WRITE
    requires_confirmation = True

    async def execute(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        node_id = kwargs.get("node_id")
        if not node_id:
            return ToolResult.fail("missing_node_id", llm_visible="node_id 必填。")

        node = await ctx.db.get(TreeNode, node_id)
        if node is None:
            return ToolResult.fail("not_found", llm_visible=f"node {node_id} 不存在。")
        if node.level_type != LevelType.TESTCASE:
            return ToolResult.fail(
                "not_a_testcase",
                llm_visible=(
                    f"node {node_id} 是 {node.level_type.value if hasattr(node.level_type,'value') else node.level_type},"
                    " 不是 TESTCASE 葉節點。請先用 create_tree_node 建一個 TESTCASE 節點。"
                ),
            )

        # IDOR + 寫權限防護(對齊 router PUT /api/testcases/{node_id})
        try:
            await ensure_project_writable(ctx.db, node.project_id, ctx.user)
        except HTTPException as e:
            return ToolResult.fail(
                "not_writable",
                llm_visible=str(e.detail) if e.detail else "此 testcase 不在你的可寫範圍。",
            )

        # 鎖:approved 的不可改(沿用 router 邏輯)
        try:
            await review_service.ensure_not_approved(
                ctx.db,
                entity_type=ReviewableEntityType.TESTCASE,
                entity_id=node_id,
                organization_id=None if ctx.user.is_superuser else ctx.organization_id,
            )
        except HTTPException as e:
            return ToolResult.fail(
                f"review_locked: {e.detail}",
                llm_visible=f"此 testcase 已被審核 approved,不能改;先請使用者 revert:{e.detail}",
            )

        # upsert TestcaseContent
        existing = (
            await ctx.db.execute(
                select(TestcaseContent).where(TestcaseContent.node_id == node_id)
            )
        ).scalar_one_or_none()
        if existing is None:
            existing = TestcaseContent(node_id=node_id)
            ctx.db.add(existing)

        ac_text = kwargs.get("ac_text")
        setup_text = kwargs.get("setup_text")
        steps_json = kwargs.get("steps_json")
        if ac_text is not None:
            existing.ac_text = ac_text
        if setup_text is not None:
            existing.setup_text = setup_text
        if steps_json is not None:
            existing.steps_json = steps_json
        await ctx.db.flush()
        await ctx.db.refresh(existing)
        await ctx.db.commit()

        payload = {
            "status": "updated",
            "node_id": node_id,
            "node_name": node.name,
            "steps_count": len(existing.steps_json or []),
            "has_ac": bool(existing.ac_text),
            "has_setup": bool(existing.setup_text),
        }
        return ToolResult.ok(json.dumps(payload, ensure_ascii=False))
