"""_is_query_searchable heuristic 單元測試(PR6)。

跳過 mem0 search 的條件:太短 / 純標點 / 純空白 — 避免雜訊召回 + 浪費 LLM quota。
"""
from __future__ import annotations

import pytest

from app.routers.hermes import _is_query_searchable


@pytest.mark.parametrize("query,expected", [
    # 跳過(太短)
    ("", False),
    (" ", False),
    ("ok", False),
    ("OK", False),
    ("hi!", False),       # 3 字
    ("   ", False),       # 只有 whitespace
    ("...", False),       # 純標點
    ("???", False),
    ("!@#$", False),      # 純符號(沒 word char)
    # 應觸發 search
    ("test", True),       # 4 字英文 — 邊界
    ("hello world", True),
    ("幫我設計測試", True),  # 中文(一-鿿)
    ("我喜歡 Pytest", True),  # 中英混
    ("a-b-c-d", True),     # 4+ 字含標點(有 word char)
    ("test  ", True),      # 後面 whitespace 不影響
    ("\n\ttest\n", True),  # 前後 whitespace
])
def test_is_query_searchable(query: str, expected: bool) -> None:
    assert _is_query_searchable(query) is expected, \
        f"query={query!r} expected={expected}"
