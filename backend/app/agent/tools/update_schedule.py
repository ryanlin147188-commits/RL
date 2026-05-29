"""update_schedule + delete_schedule tools — 完整 schedule CRUD 的 update / delete 段。

對齊 PUT / DELETE /api/schedules/{schedule_id}。
與既有 [manage_schedule.py](manage_schedule.py)(CreateScheduleTool + QuerySchedulesTool)
組成完整 CRUD,讓 schedule-ops platform skill 可以完成所有排程操作。

紅線:
* requires_confirmation=True(會改變未來排程觸發行為)
* tenant scope:用 TenantQuery 確保跨 org 不可改 / 刪
* next_run_at 修改時做時間驗證
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy import select

from app.agent.tools.base import Tool, ToolContext, ToolResult
from app.auth.permissions_catalog import P
from app.auth.tenant import TenantQuery
from app.models.schedule import RepeatType, Schedule


def _resolve_schedule(ctx: ToolContext, schedule_id: str):
    """共用:取 schedule 並做 tenant scope 檢查。回 (Schedule | None, error_visible | None)。"""
    return None


class UpdateScheduleTool(Tool):
    name = "update_schedule"
    description = (
        "更新既有排程。可改 name / next_run_at / repeat_type / repeat_config / "
        "active(啟用/暫停)/ execution_mode。"
        " **destructive**:會改變未來排程觸發行為(觸發時機 / 是否觸發 / 跑什麼)。"
        " 暫停排程請設 active=false(保留設定);徹底停止用 delete_schedule。"
        " requires_confirmation=true。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "schedule_id": {"type": "string", "description": "目標排程 UUID"},
            "name": {"type": "string", "maxLength": 200},
            "next_run_at": {
                "type": "string",
                "description": "ISO 8601 UTC,例 2026-06-01T09:00:00",
            },
            "repeat_type": {
                "type": "string",
                "enum": ["ONCE", "DAILY", "WEEKLY", "MONTHLY"],
            },
            "repeat_config": {
                "type": "string",
                "description": (
                    "WEEKLY:逗號分隔 weekday index(0=Sun..6=Sat),例 '1,3,5';"
                    "MONTHLY:日期 1-28;ONCE / DAILY 留空"
                ),
            },
            "active": {
                "type": "boolean",
                "description": "false = 暫停(保留設定不觸發);true = 啟用",
            },
            "execution_mode": {
                "type": "string",
                "enum": ["docker", "local"],
            },
        },
        "required": ["schedule_id"],
        "additionalProperties": False,
    }
    casbin_permission = P.TESTCASE_EXECUTE
    requires_confirmation = True

    async def execute(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        schedule_id = (kwargs.get("schedule_id") or "").strip()
        if not schedule_id:
            return ToolResult.fail("missing_schedule_id", llm_visible="schedule_id 必填。")

        # tenant scope query — 自動限縮在 caller 的 org
        stmt = TenantQuery.for_(Schedule).where(Schedule.id == schedule_id)
        schedule = (await ctx.db.execute(stmt)).scalar_one_or_none()
        if schedule is None:
            return ToolResult.fail(
                "schedule_not_found",
                llm_visible=f"schedule {schedule_id} 不存在或不在你的存取範圍內。",
            )

        changed: list[str] = []

        if "name" in kwargs and kwargs["name"] is not None:
            new_name = (kwargs["name"] or "").strip()
            if new_name and new_name != schedule.name:
                schedule.name = new_name
                changed.append("name")

        if "next_run_at" in kwargs and kwargs["next_run_at"]:
            try:
                next_run = datetime.fromisoformat(
                    kwargs["next_run_at"].replace("Z", "+00:00")
                )
                if next_run.tzinfo is not None:
                    next_run = next_run.astimezone(tz=None).replace(tzinfo=None)
            except ValueError as e:
                return ToolResult.fail(
                    f"invalid_next_run_at: {e}",
                    llm_visible=(
                        f"next_run_at 不是合法 ISO 8601 時間字串:{e}"
                    ),
                )
            schedule.next_run_at = next_run
            changed.append("next_run_at")

        if "repeat_type" in kwargs and kwargs["repeat_type"]:
            try:
                rt = RepeatType(kwargs["repeat_type"].upper())
            except ValueError:
                return ToolResult.fail(
                    f"invalid_repeat_type: {kwargs['repeat_type']!r}",
                    llm_visible=(
                        f"repeat_type 必須是 ONCE/DAILY/WEEKLY/MONTHLY"
                    ),
                )
            schedule.repeat_type = rt
            changed.append("repeat_type")

        if "repeat_config" in kwargs:
            # 允許清空(改為 ONCE/DAILY 時)
            schedule.repeat_config = kwargs["repeat_config"] or None
            changed.append("repeat_config")

        # 修改後若 repeat_type 是 WEEKLY/MONTHLY,需要 repeat_config
        if schedule.repeat_type == RepeatType.WEEKLY and not schedule.repeat_config:
            return ToolResult.fail(
                "missing_repeat_config",
                llm_visible="WEEKLY 排程必須提供 repeat_config(例 '1,3,5')。",
            )
        if schedule.repeat_type == RepeatType.MONTHLY and not schedule.repeat_config:
            return ToolResult.fail(
                "missing_repeat_config",
                llm_visible="MONTHLY 排程必須提供 repeat_config(例 '15')。",
            )

        if "active" in kwargs and kwargs["active"] is not None:
            schedule.active = bool(kwargs["active"])
            changed.append("active")

        if "execution_mode" in kwargs and kwargs["execution_mode"]:
            mode = kwargs["execution_mode"].lower()
            if mode not in ("docker", "local"):
                return ToolResult.fail(
                    f"invalid_execution_mode: {mode!r}",
                    llm_visible="execution_mode 必須是 docker / local",
                )
            schedule.execution_mode = mode
            changed.append("execution_mode")

        if not changed:
            return ToolResult.ok(
                json.dumps(
                    {"status": "no_change", "schedule_id": schedule_id},
                    ensure_ascii=False,
                )
            )

        await ctx.db.commit()
        await ctx.db.refresh(schedule)

        payload = {
            "status": "updated",
            "schedule_id": schedule.id,
            "name": schedule.name,
            "next_run_at": schedule.next_run_at.isoformat()
            if schedule.next_run_at
            else None,
            "repeat_type": schedule.repeat_type.value,
            "repeat_config": schedule.repeat_config,
            "active": schedule.active,
            "execution_mode": schedule.execution_mode,
            "changed_fields": changed,
        }
        return ToolResult.ok(json.dumps(payload, ensure_ascii=False))


class DeleteScheduleTool(Tool):
    name = "delete_schedule"
    description = (
        "刪除一個排程(該排程從此不會再觸發,且設定無法復原)。"
        " 若只是要暫停建議改用 update_schedule(active=false)。"
        " **極度 destructive**:requires_confirmation=true。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "schedule_id": {"type": "string", "description": "目標排程 UUID"},
        },
        "required": ["schedule_id"],
        "additionalProperties": False,
    }
    casbin_permission = P.TESTCASE_EXECUTE
    requires_confirmation = True

    async def execute(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        schedule_id = (kwargs.get("schedule_id") or "").strip()
        if not schedule_id:
            return ToolResult.fail("missing_schedule_id", llm_visible="schedule_id 必填。")

        stmt = TenantQuery.for_(Schedule).where(Schedule.id == schedule_id)
        schedule = (await ctx.db.execute(stmt)).scalar_one_or_none()
        if schedule is None:
            return ToolResult.fail(
                "schedule_not_found",
                llm_visible=f"schedule {schedule_id} 不存在或不在你的存取範圍內。",
            )

        original_name = schedule.name
        original_next_run = (
            schedule.next_run_at.isoformat() if schedule.next_run_at else None
        )
        await ctx.db.delete(schedule)
        await ctx.db.commit()

        payload = {
            "status": "deleted",
            "schedule_id": schedule_id,
            "name": original_name,
            "last_next_run_at": original_next_run,
        }
        return ToolResult.ok(json.dumps(payload, ensure_ascii=False))
