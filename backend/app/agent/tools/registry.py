"""Tool 全域 registry。

各 tool 模組 import 時呼叫 ``REGISTRY.register(MyTool())`` 註冊;``app.agent.tools.__init__``
負責 import 各 tool 子模組以觸發註冊。

Registry 內目前是 in-process dict;Phase 1c 起若要支援「per-org enable/disable tool」
會在 service 層加一層 filter,而不污染這層 registry。
"""
from __future__ import annotations

from typing import Optional

from app.agent.tools.base import Tool
from app.llm.base import ToolSpec


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(
                f"Tool 名稱重複:{tool.name!r} 已被 {type(self._tools[tool.name]).__name__} 註冊"
            )
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> None:
        """測試用:卸載一個 tool。production 不該呼叫。"""
        self._tools.pop(name, None)

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def all_tools(self) -> list[Tool]:
        return list(self._tools.values())

    def list_toolspecs(self) -> list[ToolSpec]:
        """給 chat(tools=...) 用的 ToolSpec 清單。"""
        return [t.to_toolspec() for t in self._tools.values()]

    def clear(self) -> None:
        """測試用,清空 registry。"""
        self._tools.clear()


# 全域單例(單程序內共用)
REGISTRY = ToolRegistry()
