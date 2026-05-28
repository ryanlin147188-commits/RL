"""create_tree_node tool — 建測試樹任一層節點(Feature/Platform/Page/Scenario/Testcase)。

RL 把測試案例組織成 5 層樹:
    project → Feature → Platform → Page → Scenario → Testcase(leaf)

LLM 不需要記憶層級規則:傳 ``parent_id`` 與 ``name``,backend 自動算
``level_type``(``get_expected_level``)。Testcase 是 leaf,建完後可用既有
UI 編輯步驟內容(或下一輪加 ``update_testcase_steps`` tool)。

Destructive(寫 DB)→ requires_confirmation=True;
casbin_permission=P.TESTCASE_WRITE(同 RL router /nodes 等級)。
"""
from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import HTTPException

from app.agent.tools.base import Tool, ToolContext, ToolResult
from app.auth.permissions_catalog import P
from app.auth.scope import ensure_project_writable
from app.models.tree_node import LevelType, TreeNode
from app.services.tree_service import get_expected_level

log = logging.getLogger(__name__)


class CreateTreeNodeTool(Tool):
    name = "create_tree_node"
    description = (
        "在 RL 專案內建立測試樹節點(自動算層級)。樹從上到下層級:"
        " Feature → Platform → Page → Scenario → Testcase(葉)。"
        " parent_id 留空 = 建 Feature 根層;傳 parent_id = 自動建下一層"
        "(例如 parent 是 Scenario 就建 Testcase)。"
        " **這是 destructive 動作**,requires_confirmation=true。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "project_id": {
                "type": "string",
                "description": "目標專案 ID(UUID,必填)",
            },
            "name": {
                "type": "string",
                "maxLength": 300,
                "description": "節點名稱(必填,例 'Login Page' / 'Login Success Case')",
            },
            "parent_id": {
                "type": "string",
                "description": "父節點 ID;留空 = 建 Feature 根層",
            },
            "sort_order": {
                "type": "integer",
                "description": "排序值(預設 0)",
            },
        },
        "required": ["project_id", "name"],
        "additionalProperties": False,
    }
    casbin_permission = P.TESTCASE_WRITE
    requires_confirmation = True

    async def execute(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        project_id = kwargs.get("project_id")
        name = (kwargs.get("name") or "").strip()
        parent_id = kwargs.get("parent_id") or None
        sort_order = int(kwargs.get("sort_order") or 0)

        if not (project_id and name):
            return ToolResult.fail(
                "missing_required",
                llm_visible="project_id 與 name 為必填欄位。",
            )

        # 沿用 router 的 IDOR + writability 守門
        try:
            await ensure_project_writable(ctx.db, project_id, ctx.user)
        except HTTPException as e:
            return ToolResult.fail(
                f"project_unwritable: {e.detail}",
                llm_visible=f"無法寫入此 project:{e.detail}",
            )

        # 自動算 level_type — LLM 不需要記層級規則
        try:
            expected_level = await get_expected_level(ctx.db, project_id, parent_id)
        except HTTPException as e:
            return ToolResult.fail(
                f"invalid_parent: {e.detail}",
                llm_visible=f"parent_id 不合法:{e.detail}",
            )

        node = TreeNode(
            project_id=project_id,
            parent_id=parent_id,
            level_type=expected_level,
            name=name,
            sort_order=sort_order,
        )
        ctx.db.add(node)
        await ctx.db.flush()
        await ctx.db.refresh(node)

        # Testcase 葉節點走 entity_version snapshot(對齊 routers/tree_nodes.py
        # 的 AB 表設計;標記 source=AI 給審核時知道是 AI 建的)
        if expected_level == LevelType.TESTCASE:
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
            except Exception:  # noqa: BLE001 — snapshot 失敗仍保留 node
                log.exception("entity_version snapshot failed for %s", node.id)

        await ctx.db.commit()

        level_value = (
            expected_level.value if hasattr(expected_level, "value") else str(expected_level)
        )
        payload = {
            "status": "created",
            "node_id": node.id,
            "project_id": node.project_id,
            "parent_id": node.parent_id,
            "level_type": level_value,
            "name": node.name,
            "is_leaf_testcase": expected_level == LevelType.TESTCASE,
            "next_step": (
                "若這是 Testcase(leaf)節點,使用者要去 UI 編輯步驟內容(Robot syntax);"
                " 若是非 leaf,可繼續 create_tree_node 建下一層,parent_id 帶這個 node_id。"
            ),
            "view_url": f"/#/projects/{node.project_id}",
        }
        return ToolResult.ok(
            json.dumps(payload, ensure_ascii=False),
            node_id=node.id,
            level=level_value,
        )
