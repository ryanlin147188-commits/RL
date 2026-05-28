"""update_tree_node tool — 改 tree node 名稱 / 排序。

對齊既有 PATCH /api/nodes/{id} 的 partial update 邏輯;只支援 name + sort_order
(既有 router 也沒支援改 parent_id 做 move,要動 hierarchy 屬另一個 tool)。
"""
from __future__ import annotations

import json
from typing import Any

from app.agent.tools.base import Tool, ToolContext, ToolResult
from app.auth.permissions_catalog import P
from app.auth.scope import ensure_project_in_scope
from app.models.tree_node import LevelType, TreeNode


class UpdateTreeNodeTool(Tool):
    name = "update_tree_node"
    description = (
        "更新測試樹節點的 name 或 sort_order(局部更新)。"
        " 不能改 level_type 或 parent_id(若需要移動到別的分支,先 delete 再 create)。"
        " **這是 destructive 動作**,requires_confirmation=true。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "node_id": {"type": "string", "description": "目標節點 ID(UUID)"},
            "name": {"type": "string", "maxLength": 300, "description": "新名稱"},
            "sort_order": {"type": "integer", "description": "新排序值"},
        },
        "required": ["node_id"],
        "additionalProperties": False,
    }
    casbin_permission = P.TESTCASE_WRITE
    requires_confirmation = True

    async def execute(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        node_id = kwargs.get("node_id")
        new_name = kwargs.get("name")
        new_sort = kwargs.get("sort_order")
        if not node_id:
            return ToolResult.fail("missing_node_id", llm_visible="node_id 必填。")
        if new_name is None and new_sort is None:
            return ToolResult.fail(
                "nothing_to_update",
                llm_visible="至少要提供 name 或 sort_order 其中一個。",
            )

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

        changed = False
        if new_name is not None and new_name != node.name:
            node.name = new_name
            changed = True
        if new_sort is not None and int(new_sort) != node.sort_order:
            node.sort_order = int(new_sort)
            changed = True
        await ctx.db.flush()

        # testcase 葉節點走 snapshot — 對齊 router 邏輯
        if changed and node.level_type == LevelType.TESTCASE:
            try:
                from app.services import entity_version_service as evs
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
            "status": "updated" if changed else "no_change",
            "node_id": node.id,
            "name": node.name,
            "sort_order": node.sort_order,
            "level_type": node.level_type.value if hasattr(node.level_type, "value") else str(node.level_type),
        }
        return ToolResult.ok(json.dumps(payload, ensure_ascii=False), changed=changed)
