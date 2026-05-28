"""Tool 模組 — import 各 tool 子模組以註冊到 REGISTRY。

新增 tool 的兩步:
1. 在這個目錄下新增 ``my_tool.py``,定義 ``class MyTool(Tool)``。
2. 在這個檔案 import 子模組 + 註冊 instance。

不在這層判斷「該 org 啟用了哪些 tool」— 那是 service 層 filter 的事。
這層只負責「程序內有哪些 tool 可用」。
"""
from app.agent.tools.base import Tool, ToolContext, ToolResult
from app.agent.tools.create_defect import CreateDefectTool
from app.agent.tools.manage_schedule import CreateScheduleTool, QuerySchedulesTool
from app.agent.tools.query_defect import QueryDefectTool
from app.agent.tools.query_report import QueryReportTool
from app.agent.tools.query_step_logs import QueryStepLogsTool
from app.agent.tools.registry import REGISTRY, ToolRegistry
from app.agent.tools.run_test_case import RunTestCaseTool
from app.agent.tools.start_recording import StartRecordingTool


def _bootstrap() -> None:
    """註冊內建 tool;測試用 ``REGISTRY.clear()`` 重置後可重呼。"""
    for cls in (
        QueryReportTool,
        QueryStepLogsTool,
        RunTestCaseTool,
        QueryDefectTool,
        CreateDefectTool,
        StartRecordingTool,
        QuerySchedulesTool,
        CreateScheduleTool,
    ):
        if REGISTRY.get(cls.name) is None:
            REGISTRY.register(cls())


_bootstrap()


__all__ = [
    "REGISTRY",
    "Tool",
    "ToolContext",
    "ToolRegistry",
    "ToolResult",
    "CreateDefectTool",
    "CreateScheduleTool",
    "QueryDefectTool",
    "QueryReportTool",
    "QueryStepLogsTool",
    "QuerySchedulesTool",
    "RunTestCaseTool",
    "StartRecordingTool",
]
