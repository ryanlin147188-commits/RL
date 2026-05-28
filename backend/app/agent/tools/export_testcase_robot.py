"""export_testcase_robot tool — 把 testcase 的 steps_json 轉成 Robot Framework 草稿。

把 leaf testcase 的 ``testcase_contents.steps_json`` + ``ddt_json`` 渲染成
人類可讀的 ``.robot`` 文本。**注意:這是「草稿」**,不是平台 robot_runner 真正
用來跑的執行檔(那一份在 ``backend/tasks/robot_runner.py::_build_robot_file``
組,包含 Browser/Requests/Database/Appium Library 載入、screenshot hook、
recording context 等執行期細節)。

用途:
* 讓使用者把測試案例帶離平台手動跑(本機 ``robot xxx.robot``)
* 給 code review / 文件附件
* 對齊「已有測試案例 → 我要拿走」的離線需求

LLM 看到回傳的是 JSON {robot_text: "...", line_count: N};metadata 額外帶
``view_url`` 給前端直接放下載連結(目前 view_url 還是用該 project 編輯頁,
真正下載需要前端讀 robot_text 再 blob URL — 沿用既有 import_export 風格)。

requires_confirmation=false — 純讀取 + 渲染,不寫 DB / 不觸發容器,沒風險。
"""
from __future__ import annotations

import json
import re
from typing import Any, Iterable

from fastapi import HTTPException

from app.agent.tools.base import Tool, ToolContext, ToolResult
from app.auth.permissions_catalog import P
from app.auth.scope import ensure_project_in_scope
from app.models.testcase_content import TestcaseContent
from app.models.tree_node import LevelType, TreeNode


# Robot Framework cell separator(4 個空白)— 對齊 robot_runner 風格
_RF_SEP = "    "


def _rf_escape(value: Any) -> str:
    """把使用者輸入轉成 Robot Framework cell 內安全字串。

    Robot 用 4-空白分隔 cell;cell 內若有連續多個空白會被吃掉,所以保守起見只
    把 ``$`` / ``\\`` 跳脫(Robot 變數展開 / 跳脫字元),以及把換行壓回單行。
    """
    s = str(value or "")
    s = s.replace("\\", "\\\\").replace("\r\n", "\n").replace("\n", " ")
    return s


def _step_field(step: dict, *aliases: str) -> str:
    """從 step dict 依別名順序取值,撈到第一個非空就回。

    對齊 ``markdown_service._STEP_FIELD_ALIASES`` — 平台兩套 schema 都要相容
    (bdd/keyword、step_desc/description/desc、operator/condition/compare 等)。
    """
    for key in aliases:
        v = step.get(key)
        if v not in (None, ""):
            return str(v)
    return ""


def _render_step_lines(idx: int, step: dict) -> Iterable[str]:
    """把一個 step 渲染成 1+ 行 Robot syntax。

    產出格式(縮排 4 空白):
        # Step <idx>: <bdd> <step_desc>
        Log    <action> <locator> input=<input> expected=<expected>
        # ↑ 草稿:實際執行請用平台 run_test_case(走 robot_runner)

    為什麼不直接展開成 ``Click ${loc}`` 之類:
    * action 字串實際是平台內部 DSL(``Http.Get`` / ``Db.Query`` / ``Mobile.Tap``),
      不是 Robot keyword 名稱 — 直接展開要重做完整的 robot_runner 路由,
      tool 範圍 hold 不住。
    * 用 ``Log`` 包成可執行的 placeholder 讓使用者能 ``robot file.robot`` 跑通、
      讀懂結構,需要實際斷言時再手動改。
    """
    bdd = _step_field(step, "bdd", "keyword")
    desc = _step_field(step, "step_desc", "description", "desc")
    action = _step_field(step, "action")
    locator = _step_field(step, "locator", "loc")
    inp = _step_field(step, "input")
    op = _step_field(step, "operator", "condition", "compare")
    expected = _step_field(step, "expected")

    header = " ".join(p for p in (bdd, desc) if p) or f"Step {idx}"
    yield f"{_RF_SEP}# Step {idx}: {_rf_escape(header)}"
    parts = [f"action={_rf_escape(action) or '-'}"]
    if locator:
        parts.append(f"locator={_rf_escape(locator)}")
    if inp:
        parts.append(f"input={_rf_escape(inp)}")
    if op or expected:
        parts.append(
            f"expected={_rf_escape(op + ' ' if op else '')}{_rf_escape(expected)}"
        )
    yield f"{_RF_SEP}Log    " + _RF_SEP.join(parts)


_SAFE_NAME = re.compile(r"[^A-Za-z0-9_一-鿿 -]+")


def _safe_case_name(name: str) -> str:
    """testcase 名稱可能含亂七八糟字元 — Robot Test Case 名稱允許大部分 unicode,
    但安全起見只保留 alnum / 連字號 / 底線 / 空白 / 中文,其他壓成空。"""
    cleaned = _SAFE_NAME.sub("", (name or "").strip())
    return cleaned[:200] or "Untitled Test Case"


class ExportTestcaseRobotTool(Tool):
    name = "export_testcase_robot"
    description = (
        "把 leaf testcase 的步驟匯出成 Robot Framework .robot 草稿文本"
        "(含 Settings / Variables / Test Cases 三大區塊)。"
        " **不是執行檔** — 真要在平台上跑請用 run_test_case;這份是給"
        " 離線 robot CLI / 文件附件用。回傳 JSON 內含 robot_text 全文。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "node_id": {
                "type": "string",
                "description": "目標 tree_node UUID(level_type 必須是 testcase)",
            },
        },
        "required": ["node_id"],
        "additionalProperties": False,
    }
    casbin_permission = P.TESTCASE_READ
    requires_confirmation = False

    async def execute(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        node_id = (kwargs.get("node_id") or "").strip()
        if not node_id:
            return ToolResult.fail("missing_node_id", llm_visible="node_id 必填。")

        node = await ctx.db.get(TreeNode, node_id)
        if node is None:
            return ToolResult.fail(
                "not_found", llm_visible=f"node {node_id} 不存在。",
            )

        try:
            await ensure_project_in_scope(
                ctx.db, node.project_id, ctx.user,
                not_found_detail="node not in your scope",
            )
        except HTTPException as e:
            return ToolResult.fail(
                f"out_of_scope: {e.detail}",
                llm_visible=f"node {node_id} 不在你的可見範圍內。",
            )

        if node.level_type != LevelType.TESTCASE:
            return ToolResult.fail(
                "not_a_testcase_leaf",
                llm_visible=(
                    f"node {node_id} 不是 testcase 葉節點"
                    f"(level={node.level_type.value});只有 testcase 葉節點"
                    " 有 steps_json 可匯出。"
                ),
            )

        content = await ctx.db.get(TestcaseContent, node_id)
        steps = (content.steps_json if content else None) or []
        ddt = (content.ddt_json if content else None) or {}
        ac_text = (content.ac_text if content else None) or ""

        case_name = _safe_case_name(node.name)

        lines: list[str] = []
        lines.append("*** Settings ***")
        if ac_text:
            for line in ac_text.splitlines():
                lines.append(f"Documentation    {_rf_escape(line)}")
        else:
            lines.append(f"Documentation    Exported from AutoTest node {node_id}")
        lines.append("Library    Collections")
        lines.append("Library    OperatingSystem")
        lines.append("")

        # DDT headers → suite variables(讓 ${var} 在 step expected/input 內可展開)
        ddt_headers = ddt.get("headers") or []
        ddt_rows = ddt.get("rows") or []
        if ddt_headers:
            lines.append("*** Variables ***")
            lines.append(
                f"# DDT headers: {', '.join(str(h) for h in ddt_headers)}"
            )
            lines.append(
                f"# DDT rows: {len(ddt_rows)} 筆(平台執行時會展開成多個 Test Case)"
            )
            lines.append("")

        lines.append("*** Test Cases ***")
        lines.append(case_name)
        lines.append(
            f"{_RF_SEP}[Documentation]    "
            f"草稿;實際執行請走平台 run_test_case(來源 node_id={node_id})"
        )
        if not steps:
            lines.append(f"{_RF_SEP}Log    (此 testcase 尚未編輯任何 step)")
        else:
            for idx, step in enumerate(steps, start=1):
                if not isinstance(step, dict):
                    continue
                for line in _render_step_lines(idx, step):
                    lines.append(line)
        lines.append("")

        robot_text = "\n".join(lines)
        payload = {
            "status": "exported",
            "node_id": node_id,
            "case_name": case_name,
            "step_count": len(steps),
            "ddt_row_count": len(ddt_rows),
            "line_count": len(lines),
            "robot_text": robot_text,
            "note": (
                "此為人類可讀的草稿(每個 step 用 Log 包成 placeholder),"
                " 平台正式執行請用 run_test_case tool(走 robot_runner)。"
            ),
        }
        return ToolResult.ok(
            json.dumps(payload, ensure_ascii=False),
            view_url=f"/#/projects/{node.project_id}",
            node_id=node_id,
        )
