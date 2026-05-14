"""Markdown ↔ TestcaseContent (steps_json + ddt_json) bidirectional converter.

The Markdown DSL is the autotest 7-column format (single source of truth):

    | BDD | 步驟說明 | 動作 (官方指令) | 測試目標 | 輸入 | 比較條件 | 預期值 |

Multiple ``;;``-separated values in the *輸入* column become Robot
positional arguments; ``name=value`` tokens become named arguments.
The *測試目標* column maps to ``locator``; *比較條件* + *預期值*
together form the assertion (operator + expected).

Header lines understood when parsing:
* ``Test Case: <name>``
* ``Documentation: <text>``
* ``DDT Headers: $h1,$h2`` (optional)
* ``DDT Rows:``                         ← following table is the dataset
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Tuple

STEP_CELL_COUNT = 7
INPUT_SEPARATOR = ";;"
ASSERTION_OPERATORS = ("==", "!=", "contains", "not contains", ">", ">=", "<", "<=")


# ── Importer ──────────────────────────────────────────────────────────


def _normalize_step_cells(cells: List[str]) -> List[str]:
    normalized = [c.strip() for c in cells]
    return (normalized + [""] * STEP_CELL_COUNT)[:STEP_CELL_COUNT]


def _is_separator_row(cells: List[str]) -> bool:
    return all(c and set(c) <= {"-", ":"} for c in cells)


def _parse_table_block(lines: List[str]) -> List[List[str]]:
    """Return matrix of cell strings (excluding header & separator rows)."""
    rows: List[List[str]] = []
    seen_header = False
    for raw in lines:
        line = raw.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if not cells:
            continue
        if _is_separator_row(cells):
            continue
        if not seen_header:
            seen_header = True
            continue
        rows.append(cells)
    return rows


def parse_markdown(md_text: str) -> Dict[str, Any]:
    """Parse a Markdown testcase document → dict ready to upsert into DB.

    Returns a dict with keys ``test_case_name``, ``ac_text``,
    ``steps_json`` and ``ddt_json``.
    """
    test_case_name = ""
    documentation_lines: List[str] = []
    ddt_headers: List[str] = []

    step_table_lines: List[str] = []
    ddt_table_lines: List[str] = []

    section: str = "header"  # header | steps | ddt | doc

    for raw_line in md_text.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()

        if stripped.startswith("Test Case:"):
            test_case_name = stripped.split(":", 1)[1].strip()
            continue
        if stripped.startswith("Documentation:"):
            documentation_lines.append(stripped.split(":", 1)[1].strip())
            section = "doc"
            continue
        if stripped.startswith("DDT Headers:"):
            value = stripped.split(":", 1)[1].strip()
            ddt_headers = [h.strip() for h in value.split(",") if h.strip()]
            continue
        if stripped.startswith("## "):
            heading = stripped[3:].strip().lower()
            if "ddt" in heading or heading.startswith("data"):
                section = "ddt"
            elif heading.startswith("step") or "步驟" in heading:
                section = "steps"
            else:
                section = "doc"
            continue

        if stripped.startswith("|"):
            # default unmarked tables go to steps
            if section in ("header", "doc"):
                section = "steps"
            (ddt_table_lines if section == "ddt" else step_table_lines).append(line)
            continue

        if section == "doc" and stripped:
            documentation_lines.append(stripped)

    if not step_table_lines:
        raise ValueError("Markdown 內找不到任何步驟表格")

    raw_step_rows = _parse_table_block(step_table_lines)
    steps_json: List[Dict[str, Any]] = []
    for index, cells in enumerate(raw_step_rows, start=1):
        bdd, step_desc, action, target, input_data, operator, expected = _normalize_step_cells(cells)
        steps_json.append(
            {
                "id": f"s{index:03d}",
                # 同時提供兩套欄位名稱（bdd/step_desc/operator 與 keyword/description/condition），
                # 是因為早期匯出 MD 與當前 index.html 使用前者，部分歷史錄製腳本 / 既有測試
                # 與某些步驟編輯器（含外部整合工具）使用後者。雙寫入可確保任一讀取端都能拿到值。
                "bdd": bdd,
                "step_desc": step_desc,
                "keyword": bdd,
                "description": step_desc,
                "action": action,
                "locator": target,
                "input": input_data,
                "operator": operator,
                "condition": operator,
                "expected": expected,
            }
        )

    ddt_json: Dict[str, Any] = {}
    if ddt_table_lines:
        ddt_rows = _parse_table_block(ddt_table_lines)
        if not ddt_headers and ddt_rows:
            # try take headers from the first row of original table
            first_table_line = next((l for l in ddt_table_lines if l.strip().startswith("|")), "")
            ddt_headers = [c.strip() for c in first_table_line.strip().strip("|").split("|") if c.strip()]
        ddt_json = {"headers": ddt_headers, "rows": ddt_rows}

    return {
        "test_case_name": test_case_name,
        "ac_text": "\n".join(documentation_lines).strip() or None,
        "steps_json": steps_json,
        "ddt_json": ddt_json or None,
    }


# ── Exporter ──────────────────────────────────────────────────────────


def _cell(value: Any) -> str:
    if value is None:
        return ""
    s = str(value).replace("|", "\\|").replace("\n", " ")
    return s


# steps_json 同時存在兩套欄位名稱（bdd/step_desc/operator 與 keyword/description/condition），
# 來源視寫入端而定。render_markdown 需同時相容兩套，否則任一寫入端產生的步驟匯出 MD
# 都可能拿到空白 BDD / 步驟說明 / 比較條件欄位。
_STEP_FIELD_ALIASES: List[Tuple[str, ...]] = [
    ("bdd", "keyword"),
    ("step_desc", "description", "desc"),
    ("action",),
    ("locator", "loc"),
    ("input",),
    ("operator", "condition", "compare"),
    ("expected",),
]


def _pick(step: Dict[str, Any], aliases: Tuple[str, ...]) -> Any:
    for key in aliases:
        value = step.get(key)
        if value not in (None, ""):
            return value
    return ""


def render_markdown(*, test_case_name: str, ac_text: str | None, steps_json: List[Dict[str, Any]] | None, ddt_json: Dict[str, Any] | None) -> str:
    out: List[str] = []
    if test_case_name:
        out.append(f"Test Case: {test_case_name}")
    if ac_text:
        for line in ac_text.splitlines() or [""]:
            out.append(f"Documentation: {line}" if not line.startswith("Documentation:") else line)
    if test_case_name or ac_text:
        out.append("")

    out.append("## Steps")
    out.append("| BDD | 步驟說明 | 動作 (官方指令) | 測試目標 | 輸入 | 比較條件 | 預期值 |")
    out.append("| --- | --- | --- | --- | --- | --- | --- |")
    for step in steps_json or []:
        out.append(
            "| "
            + " | ".join(_cell(_pick(step, aliases)) for aliases in _STEP_FIELD_ALIASES)
            + " |"
        )

    if ddt_json and (ddt_json.get("rows") or []):
        headers = ddt_json.get("headers") or []
        rows = ddt_json.get("rows") or []
        out.append("")
        if headers:
            out.append(f"DDT Headers: {','.join(headers)}")
        out.append("## DDT")
        out.append("| " + " | ".join(_cell(h) for h in headers) + " |")
        out.append("| " + " | ".join(["---"] * max(1, len(headers))) + " |")
        for row in rows:
            out.append("| " + " | ".join(_cell(c) for c in row) + " |")

    return "\n".join(out) + "\n"
