"""manage_schedule tools — 查 / 建排程。

兩個 tool:
* ``QuerySchedulesTool`` — 純讀 (P.TESTCASE_EXECUTE,因為看排程屬於執行類權限)
* ``CreateScheduleTool`` — 寫一筆 Schedule。**destructive**:會在指定時刻觸發
  測試執行,所以 requires_confirmation=True

Schedule 觸發後 scheduler_loop 會自動跑測試,等同於排程版的 run_test_case;
故 permission 用 P.TESTCASE_EXECUTE 對齊。
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import desc

from app.agent.tools.base import Tool, ToolContext, ToolResult
from app.auth.permissions_catalog import P
from app.auth.tenant import TenantQuery
from app.models.schedule import RepeatType, Schedule


class QuerySchedulesTool(Tool):
    name = "query_schedules"
    description = (
        "查詢 RL 平台已建立的排程。可按專案 ID / active 狀態過濾。"
        "預設回最近 10 筆,最多 50 筆。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "project_id": {"type": "string", "description": "專案 ID"},
            "active_only": {
                "type": "boolean",
                "description": "只回啟用中的排程,預設 false",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 50,
                "description": "回傳幾筆,預設 10",
            },
        },
        "additionalProperties": False,
    }
    casbin_permission = P.TESTCASE_EXECUTE

    async def execute(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        project_id = kwargs.get("project_id")
        active_only = bool(kwargs.get("active_only", False))
        limit = int(kwargs.get("limit") or 10)
        limit = max(1, min(limit, 50))

        stmt = TenantQuery.for_(Schedule)
        if project_id:
            stmt = stmt.where(Schedule.project_id == project_id)
        if active_only:
            stmt = stmt.where(Schedule.active.is_(True))
        stmt = stmt.order_by(desc(Schedule.next_run_at)).limit(limit)

        rows = (await ctx.db.execute(stmt)).scalars().all()
        items = [
            {
                "schedule_id": r.id,
                "name": r.name,
                "project_id": r.project_id,
                "node_id": r.node_id,
                "repeat_type": r.repeat_type.value
                if hasattr(r.repeat_type, "value")
                else str(r.repeat_type),
                "repeat_config": r.repeat_config,
                "next_run_at": r.next_run_at.isoformat() if r.next_run_at else None,
                "last_run_at": r.last_run_at.isoformat() if r.last_run_at else None,
                "last_report_id": r.last_report_id,
                "active": r.active,
                "execution_mode": r.execution_mode,
            }
            for r in rows
        ]
        return ToolResult.ok(
            json.dumps(
                {"count": len(items), "schedules": items}, ensure_ascii=False
            ),
            count=len(items),
        )


class CreateScheduleTool(Tool):
    name = "create_schedule"
    description = (
        "建立一個測試排程。**這是 destructive 動作**:排程到期會自動觸發測試"
        " 執行(占容器、寫報告),使用者必須在 UI 按下「同意」才會建立。"
        " 必填 name / node_id / next_run_at(ISO 8601 UTC,例 2026-06-01T09:00:00)。"
        " repeat_type 預設 ONCE;WEEKLY/MONTHLY 需配 repeat_config。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "maxLength": 200},
            "node_id": {
                "type": "string",
                "description": "目標 testcase / 目錄節點 ID(會遞迴展開到 leaf case)",
            },
            "project_id": {
                "type": "string",
                "description": "歸屬專案 ID(必填)",
            },
            "next_run_at": {
                "type": "string",
                "description": "首次觸發時間(ISO 8601 UTC),例 2026-06-01T09:00:00",
            },
            "repeat_type": {
                "type": "string",
                "enum": ["ONCE", "DAILY", "WEEKLY", "MONTHLY"],
                "description": "重複類型;預設 ONCE",
            },
            "repeat_config": {
                "type": "string",
                "description": (
                    "WEEKLY:逗號分隔的 weekday index(0=Sun..6=Sat),例 '1,3,5';"
                    "MONTHLY:日(1-28),例 '15';ONCE/DAILY 留空"
                ),
            },
            "execution_mode": {
                "type": "string",
                "enum": ["docker", "local"],
                "description": "docker = Celery 容器跑;local = 留給本機 agent",
            },
        },
        "required": ["name", "node_id", "project_id", "next_run_at"],
        "additionalProperties": False,
    }
    casbin_permission = P.TESTCASE_EXECUTE
    requires_confirmation = True

    async def execute(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        name = (kwargs.get("name") or "").strip()
        node_id = kwargs.get("node_id")
        project_id = kwargs.get("project_id")
        next_run_str = kwargs.get("next_run_at")

        if not (name and node_id and project_id and next_run_str):
            return ToolResult.fail(
                "missing_required",
                llm_visible="name / node_id / project_id / next_run_at 為必填。",
            )

        try:
            # 接受帶 Z 後綴或 timezone offset 的 ISO 字串;統一轉成 naive UTC
            # (model 欄位是 naive DateTime)
            next_run = datetime.fromisoformat(next_run_str.replace("Z", "+00:00"))
            if next_run.tzinfo is not None:
                next_run = next_run.astimezone(tz=None).replace(tzinfo=None)
        except ValueError as e:
            return ToolResult.fail(
                f"invalid_next_run_at: {e}",
                llm_visible=(
                    f"next_run_at 不是合法 ISO 8601 時間字串(例 2026-06-01T09:00:00):{e}"
                ),
            )

        repeat_str = (kwargs.get("repeat_type") or "ONCE").upper()
        try:
            repeat_type = RepeatType(repeat_str)
        except ValueError:
            return ToolResult.fail(
                f"invalid_repeat_type: {repeat_str}",
                llm_visible=(
                    f"repeat_type 必須是 ONCE/DAILY/WEEKLY/MONTHLY,收到 {repeat_str!r}"
                ),
            )

        # WEEKLY / MONTHLY 必須提供 repeat_config
        repeat_config = kwargs.get("repeat_config") or None
        if repeat_type == RepeatType.WEEKLY and not repeat_config:
            return ToolResult.fail(
                "missing_repeat_config",
                llm_visible="WEEKLY 排程必須提供 repeat_config(例 '1,3,5')。",
            )
        if repeat_type == RepeatType.MONTHLY and not repeat_config:
            return ToolResult.fail(
                "missing_repeat_config",
                llm_visible="MONTHLY 排程必須提供 repeat_config(例 '15')。",
            )

        execution_mode = (kwargs.get("execution_mode") or "docker").lower()

        schedule = Schedule(
            id=str(uuid.uuid4()),
            name=name,
            node_id=node_id,
            project_id=project_id,
            repeat_type=repeat_type,
            repeat_config=repeat_config,
            next_run_at=next_run,
            active=True,
            execution_mode=execution_mode,
        )
        ctx.db.add(schedule)
        await ctx.db.commit()
        await ctx.db.refresh(schedule)

        payload = {
            "status": "created",
            "schedule_id": schedule.id,
            "name": schedule.name,
            "next_run_at": schedule.next_run_at.isoformat(),
            "repeat_type": schedule.repeat_type.value,
            "active": schedule.active,
            "view_url": "/#/schedules",
        }
        return ToolResult.ok(
            json.dumps(payload, ensure_ascii=False),
            schedule_id=schedule.id,
        )
