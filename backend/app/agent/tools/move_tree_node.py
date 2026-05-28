"""move_tree_node tool — 把測試樹節點搬到別的 parent 底下。

對齊新加的 POST /api/nodes/{id}/move(v1.3.x)。內含三層守門:
* 同 project 限制(跨 project 不支援)
* hierarchy 規則(層級必須對得上)
* cycle 防護(不能搬到自己 / 子孫底下)
"""
from __future__ import annotations

import json
from typing import Any

from fastapi import HTTPException

from app.agent.tools.base import Tool, ToolContext, ToolResult
from app.auth.permissions_catalog import P
from app.auth.scope import ensure_project_writable
from app.models.tree_node import LevelType, TreeNode
from app.services import entity_version_service as evs
from app.services.tree_service import get_expected_level


class MoveTreeNodeTool(Tool):
    name = "move_tree_node"
    description = (
        "把測試樹節點搬到別的 parent 之下。new_parent_id 留空 = 搬到 project 根層"
        "(只 Feature node 可以)。同 project 內 only;hierarchy 層級必須對齊;"
        "不能形成 cycle(搬到自己子孫底下)。requires_confirmation=true。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "node_id": {"type": "string", "description": "要搬的 node UUID"},
            "new_parent_id": {
                "type": "string",
                "description": "新 parent UUID;留空 = 搬到 project 根層",
            },
        },
        "required": ["node_id"],
        "additionalProperties": False,
    }
    casbin_permission = P.TESTCASE_WRITE
    requires_confirmation = True

    async def execute(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        node_id = kwargs.get("node_id")
        new_parent_id = kwargs.get("new_parent_id") or None
        if not node_id:
            return ToolResult.fail("missing_node_id", llm_visible="node_id 必填。")

        node = await ctx.db.get(TreeNode, node_id)
        if node is None:
            return ToolResult.fail("not_found", llm_visible=f"node {node_id} 不存在。")

        try:
            await ensure_project_writable(ctx.db, node.project_id, ctx.user)
        except HTTPException as e:
            return ToolResult.fail(
                f"out_of_scope: {e.detail}",
                llm_visible=f"無法寫入此 node 所屬 project:{e.detail}",
            )

        # 不能搬到自己
        if new_parent_id == node_id:
            return ToolResult.fail(
                "self_loop", llm_visible="不能把 node 搬到自己底下。",
            )

        # 新 parent 存在性 + 同 project + cycle 防護
        if new_parent_id is not None:
            new_parent = await ctx.db.get(TreeNode, new_parent_id)
            if new_parent is None:
                return ToolResult.fail(
                    "parent_not_found",
                    llm_visible=f"new_parent_id {new_parent_id} 不存在。",
                )
            if new_parent.project_id != node.project_id:
                return ToolResult.fail(
                    "cross_project",
                    llm_visible="不支援跨 project 搬移;new_parent 必須在同一 project。",
                )
            # cycle:沿著 new_parent 的祖先鏈走,若遇到 node_id 就拒
            cursor = new_parent
            while cursor is not None:
                if cursor.id == node_id:
                    return ToolResult.fail(
                        "cycle_detected",
                        llm_visible="不能搬到自己的子孫節點底下(會形成循環)。",
                    )
                if cursor.parent_id is None:
                    break
                cursor = await ctx.db.get(TreeNode, cursor.parent_id)

        # Hierarchy 層級檢查
        try:
            expected_level = await get_expected_level(
                ctx.db, node.project_id, new_parent_id
            )
        except HTTPException as e:
            return ToolResult.fail(
                f"invalid_parent: {e.detail}",
                llm_visible=f"new_parent_id 不合法:{e.detail}",
            )
        if expected_level != node.level_type:
            return ToolResult.fail(
                "level_mismatch",
                llm_visible=(
                    f"層級不符:node 是 {node.level_type.value},但 new_parent"
                    f" 底下只能放 {expected_level.value if expected_level else 'None'}。"
                ),
            )

        old_parent = node.parent_id
        node.parent_id = new_parent_id
        await ctx.db.flush()
        await ctx.db.refresh(node)

        # Testcase leaf 結構變化也 snapshot
        if node.level_type == LevelType.TESTCASE:
            try:
                await evs.snapshot(
                    ctx.db,
                    entity_type="testcase",
                    entity=node,
                    source=evs.CHANGE_SOURCE_AI,
                    status=evs.CONTENT_STATUS_AI_DRAFT,
                    by=ctx.user.username,
                )
            except Exception:  # noqa: BLE001
                pass

        await ctx.db.commit()
        payload = {
            "status": "moved",
            "node_id": node.id,
            "name": node.name,
            "level_type": node.level_type.value,
            "old_parent_id": old_parent,
            "new_parent_id": new_parent_id,
        }
        return ToolResult.ok(json.dumps(payload, ensure_ascii=False))
