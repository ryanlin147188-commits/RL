"""delete_tree_node tool — 刪一個節點以及它所有子孫。

對齊 DELETE /api/nodes/{id}(走 recursive_delete service)。
**Highly destructive** — 刪一個 Feature 會連底下整棵樹一起刪。requires_confirmation=True
是基本盤;Phase 後續可加「列出將被刪的子節點」preview 機制。
"""
from __future__ import annotations

import json
from typing import Any

from app.agent.tools.base import Tool, ToolContext, ToolResult
from app.auth.permissions_catalog import P
from app.auth.scope import ensure_project_in_scope
from app.models.tree_node import TreeNode
from app.services.tree_service import recursive_delete


class DeleteTreeNodeTool(Tool):
    name = "delete_tree_node"
    description = (
        "刪除測試樹節點及其所有子孫(CASCADE)。**極度 destructive** — "
        "刪一個 Feature 會把整棵樹下的所有 testcase 一起殺掉。"
        "requires_confirmation=true,使用者必須明確同意。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "node_id": {"type": "string", "description": "目標節點 ID(UUID)"},
        },
        "required": ["node_id"],
        "additionalProperties": False,
    }
    casbin_permission = P.TESTCASE_DELETE
    requires_confirmation = True

    async def execute(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        node_id = kwargs.get("node_id")
        if not node_id:
            return ToolResult.fail("missing_node_id", llm_visible="node_id 必填。")

        node = await ctx.db.get(TreeNode, node_id)
        if node is None:
            return ToolResult.fail("not_found", llm_visible=f"node {node_id} 不存在。")
        try:
            await ensure_project_in_scope(
                ctx.db, node.project_id, ctx.user, not_found_detail="node 不在你可存取的 project 內"
            )
        except Exception as e:  # noqa: BLE001
            return ToolResult.fail(
                f"out_of_scope: {e}",
                llm_visible="此 node 不在你可存取的 project 內。",
            )

        original_name = node.name
        original_level = node.level_type.value if hasattr(node.level_type, "value") else str(node.level_type)
        await recursive_delete(ctx.db, node_id)
        await ctx.db.commit()

        payload = {
            "status": "deleted",
            "node_id": node_id,
            "name": original_name,
            "level_type": original_level,
        }
        return ToolResult.ok(json.dumps(payload, ensure_ascii=False))
