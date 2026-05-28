"""Agent 紅線守門 — 在 tool dispatch 前的權限/安全檢查。

對應紅線設計:
* **Casbin 仍是最終權限決策者** — tool 內的 ``casbin_permission`` 透過
  ``check_tool_permission()`` 驗證,user 沒權限的 tool **連 toolspec 都不暴露**
  給 LLM(由 ``filter_tools_for_user()`` 在 send_message 前過濾),避免 LLM
  反覆嘗試呼叫然後狂被拒
* **二次確認**:``Tool.requires_confirmation=True`` 時,executor 應在實際呼叫前
  停下來等使用者按 yes/no。Phase 1b 還沒做 UI confirm flow;這裡先 audit log,
  Phase 1c 加 ``pending_action`` 表 + UI
* **Audit log**:每次 tool 執行寫一筆 ``audit_logs``(action=tool_call),
  存 tool name、args、session_id、結果摘要
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from app.agent import concurrency
from app.agent.tools.base import Tool
from app.models.role import Role

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.user import User

log = logging.getLogger(__name__)


class ToolPermissionDenied(Exception):
    """User 沒權限執行此 tool。由 executor 轉成 tool result 餵給 LLM
    (LLM 收到「你沒權限做這個」會自己換種說法回覆,優於 raise 到 HTTP 層)。"""

    def __init__(self, tool_name: str, missing: str) -> None:
        super().__init__(f"Tool {tool_name!r} 需要 {missing} 權限")
        self.tool_name = tool_name
        self.missing = missing


class ConcurrencyLimitExceeded(Exception):
    """User 已達 tool per-user in-flight 上限。LLM 看到後應收手。"""

    def __init__(self, tool_name: str, limit: int, current: int) -> None:
        super().__init__(
            f"Tool {tool_name!r} 已達 per-user 上限 ({current}/{limit});"
            "請等既有任務跑完或縮減同時派發數量"
        )
        self.tool_name = tool_name
        self.limit = limit
        self.current = current


async def check_tool_permission(
    db: "AsyncSession", user: "User", tool: Tool
) -> None:
    """Raise ``ToolPermissionDenied`` if user can't run this tool。

    複製 ``app.auth.permissions.require_permission`` 的同一套邏輯,避免走
    FastAPI Depends —— 我們在 service 層也要能檢查。superuser bypass、
    沒 casbin_permission 設定 = 開放給任何登入者。
    """
    if tool.casbin_permission is None:
        return
    if user.is_superuser:
        return

    granted: set[str] = set()
    if user.role_id is not None:
        role = await db.get(Role, user.role_id)
        if role is not None and role.permissions_json:
            granted = set(role.permissions_json)

    if tool.casbin_permission not in granted:
        raise ToolPermissionDenied(tool.name, tool.casbin_permission)


async def filter_tools_for_user(
    db: "AsyncSession", user: "User", tools: list[Tool]
) -> list[Tool]:
    """過濾出 user 有權限執行的 tool。

    讓 LLM 只看到自己能跑的 tool,避免它一直呼叫被拒的 tool 浪費 token。
    superuser / user.role 一次撈完,單一 DB 查詢。
    """
    if user.is_superuser:
        return tools

    granted: set[str] = set()
    if user.role_id is not None:
        role = await db.get(Role, user.role_id)
        if role is not None and role.permissions_json:
            granted = set(role.permissions_json)

    return [
        t
        for t in tools
        if t.casbin_permission is None or t.casbin_permission in granted
    ]


async def try_acquire_concurrency(user: "User", tool: Tool) -> tuple[bool, int]:
    """Tool 有設 ``concurrency_limit_per_user`` 才檢查。

    回 (acquired, current_count)。caller(_dispatch_tool_call)在 acquired=False
    時應寫一條 tool message 帶 ``ConcurrencyLimitExceeded`` 的訊息給 LLM。

    Fail-open(沿用 concurrency.try_acquire 的行為)— Valkey 不通時放行。
    """
    if tool.concurrency_limit_per_user is None:
        return True, 0
    return await concurrency.try_acquire(
        user.id, tool.name, limit=tool.concurrency_limit_per_user
    )


async def release_concurrency(user: "User", tool: Tool) -> None:
    """tool 失敗 / Celery 完成事件時 release。同步 tool 完成也 release。"""
    if tool.concurrency_limit_per_user is None:
        return
    await concurrency.release(user.id, tool.name)


def should_pause_for_confirmation(tool: Tool) -> bool:
    """Phase 1b stub:回傳 tool 是否需要二次確認。

    Phase 1c 會接 UI:executor 看到 True → 寫一筆 ``pending_action``,回 LLM
    「等使用者確認」訊息,前端 modal 跳出來,使用者按 yes 後 executor 才繼續。
    Phase 1b 為止所有 tool 都是 False(只有純讀 query_report),這函式回 False。
    """
    return tool.requires_confirmation


def audit_tool_call(
    *,
    user_id: str,
    session_id: str,
    tool_name: str,
    arguments: dict[str, Any],
    ok: bool,
    error: str | None = None,
) -> None:
    """寫 tool 呼叫的 audit 記錄。

    Phase 1b 暫時走 logger,Phase 1c 改寫進 audit_logs 表(既有 model)。
    這裡只 log,不該 raise:audit 失敗不能阻擋 tool 主流程。

    安全性:用 ``json.dumps`` 確保 control character(``\\n`` / ``\\r``)被
    escape,避免 LLM-controlled arguments 內含 newline 偽造額外 log 行
    (log injection / CWE-117)。
    """
    import json as _json
    try:
        args_safe = _json.dumps(arguments, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        args_safe = repr(arguments)
    payload = {
        "user_id": user_id,
        "session_id": session_id,
        "tool": tool_name,
        "ok": ok,
        "args": args_safe,  # 已 escape 的 JSON 字串,non-injectable
    }
    if error:
        # error 也可能含 LLM-controlled 內容(tool 失敗訊息),同樣 escape
        payload["error"] = _json.dumps(error, ensure_ascii=False, default=str)
    # 整個 dict 也以 JSON 形式輸出,避免 dict repr 對 string 不轉義 \n
    log.info("tool_call %s", _json.dumps(payload, ensure_ascii=False, default=str))
