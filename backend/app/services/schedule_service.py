"""排程計算與觸發邏輯。

- compute_next_run(): 根據 repeat_type / repeat_config 計算下一次觸發時間
- fire_due_schedules(): 掃描所有到期的排程，觸發執行流程並更新 next_run_at
- scheduler_loop(): 背景任務（由 lifespan 啟動），每 SCHEDULER_TICK_SECONDS 掃描一次
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

SCHEDULER_TICK_SECONDS = 30


def _parse_weekday_list(repeat_config: Optional[str]) -> list[int]:
    """WEEKLY repeat_config 格式：逗號分隔 0-6（0=星期日, 6=星期六）。"""
    if not repeat_config:
        return []
    result: list[int] = []
    for part in repeat_config.split(","):
        part = part.strip()
        if part.isdigit():
            v = int(part)
            if 0 <= v <= 6:
                result.append(v)
    return sorted(set(result))


def _parse_month_day(repeat_config: Optional[str]) -> int:
    """MONTHLY repeat_config：單一 1-31 字串。"""
    if not repeat_config:
        return 1
    try:
        v = int(str(repeat_config).strip())
        return max(1, min(31, v))
    except ValueError:
        return 1


def _py_weekday_to_sun0(d: datetime) -> int:
    """Python 的 weekday(): Mon=0..Sun=6；轉成 Sun=0..Sat=6（前端一致）。"""
    return (d.weekday() + 1) % 7


def _add_days_keeping_hm(base: datetime, days: int) -> datetime:
    return base + timedelta(days=days)


def _next_monthly(base: datetime, day: int) -> datetime:
    """回傳 `base` 之後第一個月份的 `day` 日（保留時分秒）。若當月沒這天（例如 2 月 31），取該月最後一天。"""
    import calendar

    year, month = base.year, base.month
    # 先試「同月同日」
    last_day_this_month = calendar.monthrange(year, month)[1]
    target_day = min(day, last_day_this_month)
    candidate = base.replace(day=target_day)
    if candidate > base:
        return candidate
    # 往下一個月推
    month += 1
    if month > 12:
        year += 1
        month = 1
    last_day_next_month = calendar.monthrange(year, month)[1]
    target_day = min(day, last_day_next_month)
    return base.replace(year=year, month=month, day=target_day)


def compute_next_run(
    *,
    repeat_type: str,
    repeat_config: Optional[str],
    from_time: datetime,
    start_time: datetime,
) -> Optional[datetime]:
    """
    計算下一次觸發時間。

    - repeat_type = ONCE   → 回傳 None（單次觸發不再排下一次）
    - repeat_type = DAILY  → from_time + 1 day（以 start_time 的時分秒為準）
    - repeat_type = WEEKLY → 下一個指定 weekday（以 start_time 的時分秒為準）
    - repeat_type = MONTHLY→ 下個月的指定日（以 start_time 的時分秒為準）

    `from_time` 通常是「剛剛觸發完的時間」；`start_time` 是排程原始設定時間。
    """
    rt = (repeat_type or "ONCE").upper()
    # 所有 recurring 類型都以 start_time 的時分秒為基準
    hour, minute, second = start_time.hour, start_time.minute, start_time.second

    if rt == "ONCE":
        return None

    # 從 from_time 的隔日（避免重複觸發同一時間）開始找
    cursor = from_time.replace(hour=hour, minute=minute, second=second, microsecond=0)
    if cursor <= from_time:
        cursor = _add_days_keeping_hm(cursor, 1)

    if rt == "DAILY":
        return cursor

    if rt == "WEEKLY":
        weekdays = _parse_weekday_list(repeat_config)
        if not weekdays:
            return cursor  # 沒設定星期幾，退化為每天
        for _ in range(14):
            if _py_weekday_to_sun0(cursor) in weekdays:
                return cursor
            cursor = _add_days_keeping_hm(cursor, 1)
        return cursor  # 保險：14 天內一定會找到

    if rt == "MONTHLY":
        day = _parse_month_day(repeat_config)
        return _next_monthly(from_time.replace(hour=hour, minute=minute, second=second, microsecond=0), day)

    return None


async def _trigger_schedule(
    db: AsyncSession,
    schedule,
    execution_mode: str = "docker",
) -> Optional[str]:
    """為一個 schedule 觸發一次執行，回傳建立的 report_id。

    與 executions.run_execution 類似，但 trigger_type 標成 "Scheduled"。
    execution_mode 來源：
      - 立即執行（前端手動按「立即」）：由前端傳入
      - 背景自動觸發（scheduler_loop）：目前固定為 "docker"
    """
    from app.services.execution_service import collect_testcase_ids, create_report
    import json as _json

    # 多選節點：node_ids_json 優先；否則退化為 [schedule.node_id]
    node_ids: list[str] = []
    raw_ids = getattr(schedule, "node_ids_json", None)
    if raw_ids:
        try:
            parsed = _json.loads(raw_ids)
            if isinstance(parsed, list):
                node_ids = [n for n in parsed if isinstance(n, str) and n]
        except Exception:
            node_ids = []
    if not node_ids:
        node_ids = [schedule.node_id] if schedule.node_id else []

    # 聚合所有選到節點底下的 TESTCASE ids（去重、保留第一次出現的順序）
    seen: set[str] = set()
    testcase_ids: list[str] = []
    for nid in node_ids:
        ids = await collect_testcase_ids(db, nid)
        for tid in ids:
            if tid not in seen:
                seen.add(tid)
                testcase_ids.append(tid)
    if not testcase_ids:
        logger.warning(
            "Schedule %s 的節點 %s 底下找不到 TESTCASE",
            schedule.id, node_ids,
        )
        return None

    task_id = str(uuid.uuid4())
    report = await create_report(
        db, schedule.project_id, "Scheduled", len(testcase_ids), task_id,
        execution_mode=execution_mode,
        source_node_id=schedule.node_id,
    )

    # ── 通知:排程觸發(schedule.fired)─── 給 org 內訂閱者
    try:
        from app.services.notification_dispatch import notify_broadcast
        await notify_broadcast(
            db=db,
            event_key="schedule.fired",
            organization_id=getattr(schedule, "organization_id", None) or getattr(report, "organization_id", None),
            title=f"排程觸發:{getattr(schedule, 'name', None) or schedule.id[:8]}",
            body=(
                f"排程 {getattr(schedule, 'name', '') or schedule.id} 已觸發,"
                f"共 {len(testcase_ids)} 個測試案例排入執行。"
            ),
            level="info",
            related_entity_type="report",
            related_entity_id=report.id,
        )
    except Exception:
        logger.exception("notify schedule.fired failed (schedule=%s)", schedule.id)

    # local 模式：不送 Celery，改由本機 agent 透過 /api/local-runner/claim 認領
    if (execution_mode or "docker").lower() == "local":
        logger.info("Schedule %s 以 local 模式排入，等待本機 agent 認領", schedule.id)
        return report.id

    try:
        from tasks.celery_app import celery_app

        celery_app.send_task(
            "tasks.execution_tasks.run_tests",
            kwargs={
                "task_id": task_id,
                "report_id": report.id,
                "testcase_ids": testcase_ids,
            },
        )
    except Exception as exc:  # Celery 不可用時別讓排程整個壞掉
        logger.error("Schedule %s 送入 Celery 失敗：%s", schedule.id, exc)

    return report.id


async def fire_due_schedules(db: AsyncSession) -> int:
    """掃一次 schedules 表，觸發所有到期（next_run_at <= now）且 active 的排程。

    回傳本輪觸發的筆數。
    """
    from app.models.schedule import RepeatType, Schedule

    # 使用伺服器當地時間（docker-compose 設 TZ=Asia/Taipei）；
    # 前端 datetime-local 送進來的也是本地時間，直接比較避免時區換算錯誤。
    now = datetime.now()
    result = await db.execute(
        select(Schedule).where(Schedule.active.is_(True), Schedule.next_run_at <= now)
    )
    due = list(result.scalars())
    if not due:
        return 0

    for schedule in due:
        start_time = schedule.next_run_at
        # 排程背景觸發時使用 schedule 自己儲存的執行環境
        report_id = await _trigger_schedule(
            db, schedule,
            execution_mode=getattr(schedule, "execution_mode", "docker") or "docker",
        )
        schedule.last_run_at = now
        schedule.last_report_id = report_id
        next_run = compute_next_run(
            repeat_type=schedule.repeat_type.value
            if isinstance(schedule.repeat_type, RepeatType)
            else schedule.repeat_type,
            repeat_config=schedule.repeat_config,
            from_time=now,
            start_time=start_time,
        )
        if next_run is None:
            schedule.active = False
        else:
            schedule.next_run_at = next_run

    await db.commit()
    logger.info("已觸發 %d 個排程", len(due))
    return len(due)


async def _reap_stale_running_reports(db: AsyncSession, max_age_minutes: int = 120) -> int:
    """把超過 `max_age_minutes` 仍卡在 RUNNING 的報告標記為 FAILED。

    避免 Celery 子行程卡住或 crash 時，報告永遠停在 RUNNING 狀態。
    門檻設為 2 小時，讓正常長時間跑的測試（包含排程觸發的 DDT 批次）不會被誤殺；
    若測試真的會跑 >2 小時，請在終端機手動按「停止」而不要靠這個 watchdog。
    """
    from datetime import timedelta
    from app.models.execution_report import ExecutionReport, ReportStatus

    threshold = datetime.now() - timedelta(minutes=max_age_minutes)
    result = await db.execute(
        select(ExecutionReport).where(
            ExecutionReport.status == ReportStatus.RUNNING,
            ExecutionReport.created_at <= threshold,
        )
    )
    stale = list(result.scalars())
    for report in stale:
        report.status = ReportStatus.FAILED
    if stale:
        await db.commit()
        logger.warning("已將 %d 份逾時的 RUNNING 報告標記為 FAILED", len(stale))
    return len(stale)


async def scheduler_loop():
    """背景任務：每 SCHEDULER_TICK_SECONDS 秒掃一次 schedules 表 + 清理卡住的 RUNNING 報告。"""
    from app.database import AsyncSessionLocal

    # 強制 INFO 以上都上到 uvicorn 的 stdout
    logging.basicConfig(level=logging.INFO, force=False)
    logger.info("Scheduler loop 啟動（tick=%ss）", SCHEDULER_TICK_SECONDS)
    while True:
        try:
            async with AsyncSessionLocal() as session:
                fired = await fire_due_schedules(session)
                if fired:
                    logger.info("本輪觸發 %d 個排程", fired)
            async with AsyncSessionLocal() as session:
                await _reap_stale_running_reports(session)
        except Exception as exc:
            logger.exception("Scheduler tick 失敗：%s", exc)
        try:
            await asyncio.sleep(SCHEDULER_TICK_SECONDS)
        except asyncio.CancelledError:
            break
