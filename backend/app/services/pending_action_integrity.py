"""PendingAction.arguments 的 HMAC 防竄改機制。

問題:``pending_actions.arguments`` 存在 DB,使用者按下「同意」時後端會用
DB 裡的值去重跑 tool。任何能寫該 row 的攻擊路徑(SQL injection、被接管
admin 帳號、second-order injection、XSS 之後透過 user 自己的 session 修改、
或 mem0 sidecar 之類的子服務若意外有寫權限)都能在 modal 顯示後、approve
真實執行前竄改 arguments,讓使用者「看到舊內容、執行新內容」。

解法:create() 時對 ``(tool_name, tool_call_id, session_id, canonical(args))``
做 HMAC-SHA256,把 hex 字串塞進 ``arguments["__integrity__"]``;approve 端
在執行前把 ``__integrity__`` pop 出來、重算 HMAC、與 pop 的值常數時間比對。
不符 → 拒絕並 log warning。

為什麼不開新欄位:加 alembic migration 較有部署阻力;在 ``arguments`` JSON
內存特殊 key 對外行為等價、又不必 migration。內部使用的 magic key 以雙底
線開頭(``__integrity__``),tool execute 端會把它 pop 掉再傳給 ``execute()``,
LLM / tool 看不到。

向後相容:既有舊 row(沒 ``__integrity__``)在 approve 時走 fallback 流程 —
log warning + 拒絕執行,要求使用者重新發送 prompt(因為這類 row 可能也是
被攻擊者寫進來的,寧可錯殺)。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
from typing import Any, Optional

from app.auth.security import JWT_SECRET

log = logging.getLogger(__name__)

_INTEGRITY_KEY = "__integrity__"


def _canonical(args: Optional[dict[str, Any]]) -> str:
    """產 deterministic JSON(sort_keys + 緊湊 separator),確保兩端算出來一樣。"""
    if not args:
        return "{}"
    # 排除 __integrity__ 自己,避免遞迴
    clean = {k: v for k, v in args.items() if k != _INTEGRITY_KEY}
    return json.dumps(clean, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _compute(
    *,
    tool_name: str,
    tool_call_id: str,
    session_id: str,
    args: Optional[dict[str, Any]],
) -> str:
    msg = "|".join(
        [
            "pending_action_v1",
            tool_name or "",
            tool_call_id or "",
            session_id or "",
            _canonical(args),
        ]
    ).encode("utf-8")
    return hmac.new(JWT_SECRET.encode("utf-8"), msg, hashlib.sha256).hexdigest()


def stamp(
    *,
    tool_name: str,
    tool_call_id: str,
    session_id: str,
    arguments: Optional[dict[str, Any]],
) -> dict[str, Any]:
    """回傳一份加了 ``__integrity__`` 的新 dict。原 dict 不被修改。"""
    base = dict(arguments or {})
    base[_INTEGRITY_KEY] = _compute(
        tool_name=tool_name,
        tool_call_id=tool_call_id,
        session_id=session_id,
        args=base,
    )
    return base


def verify_and_strip(
    *,
    tool_name: str,
    tool_call_id: str,
    session_id: str,
    arguments: Optional[dict[str, Any]],
) -> Optional[dict[str, Any]]:
    """驗證 HMAC 並回傳剝掉 ``__integrity__`` 的 dict。失敗回 None(caller 拒絕執行)。

    對沒有 ``__integrity__`` 欄位的舊 row 也回 None — 寧可拒絕也不放行。
    """
    if not arguments or _INTEGRITY_KEY not in arguments:
        log.warning(
            "PendingAction missing __integrity__ (tool=%s, tool_call_id=%s) — refusing",
            tool_name, tool_call_id,
        )
        return None
    presented = arguments.get(_INTEGRITY_KEY) or ""
    expected = _compute(
        tool_name=tool_name,
        tool_call_id=tool_call_id,
        session_id=session_id,
        args=arguments,
    )
    if not hmac.compare_digest(presented, expected):
        log.warning(
            "PendingAction __integrity__ mismatch (tool=%s, tool_call_id=%s) — "
            "arguments may have been tampered after create",
            tool_name, tool_call_id,
        )
        return None
    cleaned = {k: v for k, v in arguments.items() if k != _INTEGRITY_KEY}
    return cleaned
