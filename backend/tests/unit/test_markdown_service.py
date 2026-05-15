"""Unit tests for markdown_service — pure string conversion, no DB."""
from __future__ import annotations

import pytest

from app.services.markdown_service import parse_markdown, render_markdown


# ── Helpers ─────────────────────────────────────────────────────────────

def _minimal_md(rows: list[str] | None = None) -> str:
    header = "| BDD | 步驟說明 | 動作 (官方指令) | 測試目標 | 輸入 | 比較條件 | 預期值 |\n"
    sep    = "| --- | --- | --- | --- | --- | --- | --- |\n"
    body = "\n".join(rows or ["| Given | 開啟首頁 | navigate | | https://example.com | | |"])
    return header + sep + body + "\n"


# ── parse_markdown ────────────────────────────────────────────────────


class TestParseMarkdownBasic:
    def test_minimal_doc_parses_one_step(self):
        md = _minimal_md()
        result = parse_markdown(md)
        assert len(result["steps_json"]) == 1

    def test_test_case_name_extracted(self):
        md = "Test Case: 登入流程\n" + _minimal_md()
        result = parse_markdown(md)
        assert result["test_case_name"] == "登入流程"

    def test_test_case_name_empty_when_missing(self):
        result = parse_markdown(_minimal_md())
        assert result["test_case_name"] == ""

    def test_documentation_extracted(self):
        md = "Test Case: T\nDocumentation: 測試說明文字\n" + _minimal_md()
        result = parse_markdown(md)
        assert result["ac_text"] == "測試說明文字"

    def test_no_documentation_returns_none(self):
        result = parse_markdown(_minimal_md())
        assert result["ac_text"] is None

    def test_no_step_table_raises(self):
        with pytest.raises(ValueError, match="找不到任何步驟表格"):
            parse_markdown("Test Case: Empty\n")


class TestParseMarkdownStepFields:
    def test_step_fields_mapped_correctly(self):
        row = "| Given | 開啟頁面 | navigate | #url | https://example.com | == | 200 |"
        result = parse_markdown(_minimal_md([row]))
        step = result["steps_json"][0]
        assert step["bdd"] == "Given"
        assert step["step_desc"] == "開啟頁面"
        assert step["action"] == "navigate"
        assert step["locator"] == "#url"
        assert step["input"] == "https://example.com"
        assert step["operator"] == "=="
        assert step["expected"] == "200"

    def test_step_has_dual_field_names(self):
        result = parse_markdown(_minimal_md())
        step = result["steps_json"][0]
        # Both old and new field name sets present
        assert "bdd" in step and "keyword" in step
        assert "step_desc" in step and "description" in step
        assert "operator" in step and "condition" in step

    def test_step_ids_sequential(self):
        rows = [
            "| Given | Step 1 | click | | | | |",
            "| When | Step 2 | type | | hello | | |",
            "| Then | Step 3 | assert | | | == | ok |",
        ]
        result = parse_markdown(_minimal_md(rows))
        ids = [s["id"] for s in result["steps_json"]]
        assert ids == ["s001", "s002", "s003"]

    def test_short_row_pads_missing_cells(self):
        row = "| Given | Only two cells |"
        result = parse_markdown(_minimal_md([row]))
        step = result["steps_json"][0]
        assert step["bdd"] == "Given"
        assert step["action"] == ""
        assert step["expected"] == ""

    def test_multiple_steps_count(self):
        rows = ["| | Step %d | | | | | |" % i for i in range(5)]
        result = parse_markdown(_minimal_md(rows))
        assert len(result["steps_json"]) == 5


class TestParseMarkdownDDT:
    def test_ddt_section_parsed(self):
        md = (
            "Test Case: DDT Test\n"
            + _minimal_md()
            + "\nDDT Headers: $user,$pass\n"
            + "## DDT\n"
            + "| $user | $pass |\n"
            + "| --- | --- |\n"
            + "| alice | s3cr3t |\n"
            + "| bob | pa55 |\n"
        )
        result = parse_markdown(md)
        assert result["ddt_json"] is not None
        assert result["ddt_json"]["headers"] == ["$user", "$pass"]
        assert len(result["ddt_json"]["rows"]) == 2

    def test_no_ddt_returns_none(self):
        result = parse_markdown(_minimal_md())
        assert result["ddt_json"] is None


# ── render_markdown ────────────────────────────────────────────────────


class TestRenderMarkdown:
    def _basic_step(self, **kwargs):
        defaults = {
            "bdd": "Given",
            "step_desc": "開啟頁面",
            "action": "navigate",
            "locator": "",
            "input": "https://example.com",
            "operator": "",
            "expected": "",
        }
        defaults.update(kwargs)
        return defaults

    def test_output_contains_test_case_name(self):
        md = render_markdown(
            test_case_name="My Test",
            ac_text=None,
            steps_json=[self._basic_step()],
            ddt_json=None,
        )
        assert "Test Case: My Test" in md

    def test_output_contains_documentation(self):
        md = render_markdown(
            test_case_name="T",
            ac_text="Some docs",
            steps_json=[self._basic_step()],
            ddt_json=None,
        )
        assert "Documentation: Some docs" in md

    def test_output_has_steps_header(self):
        md = render_markdown(
            test_case_name="T",
            ac_text=None,
            steps_json=[self._basic_step()],
            ddt_json=None,
        )
        assert "## Steps" in md
        assert "| BDD | 步驟說明 |" in md

    def test_step_values_in_output(self):
        md = render_markdown(
            test_case_name="T",
            ac_text=None,
            steps_json=[self._basic_step(bdd="When", action="click", expected="OK")],
            ddt_json=None,
        )
        assert "| When |" in md
        assert "click" in md
        assert "OK" in md

    def test_empty_steps_renders_header_only(self):
        md = render_markdown(
            test_case_name="T",
            ac_text=None,
            steps_json=[],
            ddt_json=None,
        )
        assert "## Steps" in md

    def test_ddt_section_rendered(self):
        ddt = {"headers": ["$user", "$pass"], "rows": [["alice", "secret"]]}
        md = render_markdown(
            test_case_name="T",
            ac_text=None,
            steps_json=[self._basic_step()],
            ddt_json=ddt,
        )
        assert "## DDT" in md
        assert "DDT Headers:" in md
        assert "alice" in md

    def test_no_ddt_when_empty_rows(self):
        md = render_markdown(
            test_case_name="T",
            ac_text=None,
            steps_json=[self._basic_step()],
            ddt_json={"headers": [], "rows": []},
        )
        assert "## DDT" not in md

    def test_pipe_in_cell_escaped(self):
        md = render_markdown(
            test_case_name="T",
            ac_text=None,
            steps_json=[self._basic_step(expected="a|b")],
            ddt_json=None,
        )
        assert "a\\|b" in md

    def test_alias_fields_respected(self):
        step = {
            "keyword": "Then",
            "description": "驗證結果",
            "action": "assert",
            "loc": "#result",
            "input": "",
            "condition": "==",
            "expected": "pass",
        }
        md = render_markdown(
            test_case_name="T",
            ac_text=None,
            steps_json=[step],
            ddt_json=None,
        )
        assert "| Then |" in md
        assert "驗證結果" in md
        assert "==" in md


# ── Round-trip ────────────────────────────────────────────────────────


class TestRoundTrip:
    def test_parse_then_render_preserves_name_and_steps(self):
        original = (
            "Test Case: 登入測試\n"
            "Documentation: 描述\n"
            "\n"
            "## Steps\n"
            "| BDD | 步驟說明 | 動作 (官方指令) | 測試目標 | 輸入 | 比較條件 | 預期值 |\n"
            "| --- | --- | --- | --- | --- | --- | --- |\n"
            "| Given | 輸入帳密 | type | #user | alice | | |\n"
            "| Then | 驗證成功 | assert | #msg | | == | ok |\n"
        )
        parsed = parse_markdown(original)
        rendered = render_markdown(
            test_case_name=parsed["test_case_name"],
            ac_text=parsed["ac_text"],
            steps_json=parsed["steps_json"],
            ddt_json=parsed["ddt_json"],
        )
        re_parsed = parse_markdown(rendered)
        assert re_parsed["test_case_name"] == parsed["test_case_name"]
        assert len(re_parsed["steps_json"]) == len(parsed["steps_json"])
        for orig, reparsed in zip(parsed["steps_json"], re_parsed["steps_json"]):
            assert reparsed["bdd"] == orig["bdd"]
            assert reparsed["action"] == orig["action"]
            assert reparsed["expected"] == orig["expected"]
