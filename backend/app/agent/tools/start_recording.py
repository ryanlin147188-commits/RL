"""start_recording tool — 建立錄製階段(PENDING 狀態)。

Phase 1c-3 範圍簡化:**只建一筆 RecordingSession row(status=PENDING)**,
告訴使用者去既有 ``/#/recordings`` 頁面繼續錄製 — 不真派 recorder 容器。

派 recorder 容器是相對重的動作(VM 磁碟 / Playwright codegen 視窗),且
原本就是設計給使用者在 UI 端啟動。Phase 2+ 若要 fully 自動派,要在 tool 內
重做 [routers/recordings.py](app/routers/recordings.py) 的 spawn 邏輯,範圍
比 Phase 1c-3 大,先不做。

紅線:
* ``requires_confirmation = True``(雖然只建 DB row,但語意上「使用者授權 AI
  幫他開個錄製階段」應該明確同意)
* ``casbin_permission = P.TESTCASE_WRITE``(沿用 router 既有設定)
* ``concurrency_limit_per_user = 2``(對應你前述 spec 「recorder 上限 2」;
  雖然這版不真派容器,先把 slot 邏輯擺好,Phase 2+ 接真派時無痛升級)
"""
from __future__ import annotations

import json
import uuid
from typing import Any

from app.agent.tools.base import Tool, ToolContext, ToolResult
from app.auth.permissions_catalog import P
from app.models.recording import RecordingSession


class StartRecordingTool(Tool):
    name = "start_recording"
    description = (
        "在 RL 平台上建立一個瀏覽器錄製階段(PENDING 狀態),讓使用者接著在 "
        "/#/recordings 頁面開始錄製。**Phase 1c-3 版本不會真的派出瀏覽器容器**,"
        "只先建立 DB row;真實錄製仍需使用者去 UI 啟動。requires_confirmation=true。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "target_url": {
                "type": "string",
                "description": "要錄製的目標網址(例:https://example.com)",
            },
            "project_id": {
                "type": "string",
                "description": "歸屬專案 ID(可選)",
            },
        },
        "required": ["target_url"],
        "additionalProperties": False,
    }
    casbin_permission = P.TESTCASE_WRITE
    requires_confirmation = True
    # 雖然這版不真派容器,先設 slot 限制,Phase 2+ 接真派時不用改邏輯
    concurrency_limit_per_user = 2

    async def execute(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        target_url = (kwargs.get("target_url") or "").strip()
        if not target_url:
            return ToolResult.fail(
                "missing_target_url",
                llm_visible="target_url 為必填欄位。",
            )
        # 簡單 URL 健全性檢查(不嚴格驗 — 留給後續實作 / 既有 UI)
        if not (target_url.startswith("http://") or target_url.startswith("https://")):
            return ToolResult.fail(
                "invalid_target_url",
                llm_visible="target_url 必須以 http:// 或 https:// 開頭。",
            )

        project_id = kwargs.get("project_id") or None
        session = RecordingSession(
            id=str(uuid.uuid4()),
            project_id=project_id,
            target_url=target_url,
            status="PENDING",
        )
        ctx.db.add(session)
        await ctx.db.commit()
        await ctx.db.refresh(session)

        payload = {
            "status": "pending_recording_created",
            "recording_session_id": session.id,
            "target_url": session.target_url,
            "project_id": session.project_id,
            "next_step": (
                "Phase 1c-3 版本只建立 DB row。請告訴使用者:前往 RL 介面的"
                " /#/recordings 頁面找到這個 session(ID 已給),按下開始錄製"
                " 按鈕,瀏覽器 codegen 結束後系統會自動上傳腳本。"
            ),
            "view_url": f"/#/recordings/{session.id}",
        }
        return ToolResult.ok(
            json.dumps(payload, ensure_ascii=False),
            recording_session_id=session.id,
        )
