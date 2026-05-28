"""Agent runtime — Phase 1b。

* ``tools/``:Tool 抽象 + 各個工具實作 + registry
* ``guard.py``:Casbin / 紅線守門

Import this package at app startup to side-effect-register the built-in tools
(via ``app.agent.tools.__init__`` 中的 import order)。
"""
from app.agent import tools  # noqa: F401 — 觸發 tool 註冊

__all__ = ["tools"]
