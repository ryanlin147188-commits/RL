"""Auto-continuation listener — Phase 1c-3 收尾。

Celery worker 跑完 ``tasks.execution_tasks.run_tests`` 後不會主動通知 agent;
這個背景 task 每 N 秒輪詢:有沒有 agent_messages.task_id 對應的 ExecutionReport
從 RUNNING → PASSED/FAILED?有的話:
  1. UPDATE agent_messages.content 改成「task 完成 + 統計摘要」JSON
  2. 起新一輪 LLM chat(用 agent_service._run_chat_loop),讓 LLM 看到真結果
     自然產出總結訊息給使用者
  3. 在 Redis key ``agent:auto_cont:{message_id}`` setex 1 個月,避免重啟後重複處理

設計取捨:
* 用 **polling** 而非 Celery 信號 / pub/sub 監聽,避免動 tasks/ 目錄程式碼
* 預設 10 秒一輪;高頻會增加 DB 負擔,低頻會延遲總結回覆
* 同時多個 task 完成 → 序列化處理(_run_chat_loop 是 await),不開無上限併發 LLM
* LLM 失敗時 log + continue 下一輪 — 不卡住其他 task 完成事件
* Auto-continuation 失敗不影響使用者體驗:UI 仍能看到 tool message 更新成 completed
  JSON,只是少了「LLM 主動總結」那段;使用者下次發訊息 LLM 也會看到完整 history
"""
from __future__ import annotations

import asyncio
import json
import logging

from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models.agent_session import AgentMessage, AgentSession
from app.models.execution_report import ExecutionReport, ReportStatus
from app.models.user import User

log = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL_SEC = 10
PROCESSED_KEY_PREFIX = "agent:auto_cont:"
PROCESSED_TTL_SEC = 30 * 86400  # 30 天

# Lazy redis client(沿用 [[concurrency.py]] / [[revocation.py]] 模式)
_async_redis = None


async def _get_redis():
    global _async_redis
    if _async_redis is None:
        from redis import asyncio as aioredis

        from app.config import settings

        _async_redis = aioredis.from_url(
            settings.REDIS_URL, encoding="utf-8", decode_responses=True
        )
    return _async_redis


async def _is_processed(message_id: str) -> bool:
    """Redis key 已存在 → 之前處理過。Redis 故障 → 回 False(re-process 不致命)。"""
    try:
        client = await _get_redis()
        return bool(await client.exists(PROCESSED_KEY_PREFIX + message_id))
    except Exception:  # noqa: BLE001
        return False


async def _mark_processed(message_id: str) -> None:
    try:
        client = await _get_redis()
        await client.setex(
            PROCESSED_KEY_PREFIX + message_id, PROCESSED_TTL_SEC, "1"
        )
    except Exception as e:  # noqa: BLE001
        log.warning("auto_continuation mark_processed failed: %s", e)


async def _process_completion(
    db, msg: AgentMessage, report: ExecutionReport
) -> None:
    """單筆完成事件:update tool message + 跑 follow-up chat。"""
    session = await db.get(AgentSession, msg.session_id)
    if session is None:
        log.warning(
            "auto_continuation: session %s missing for message %s",
            msg.session_id,
            msg.id,
        )
        return
    user = await db.get(User, session.user_id)
    if user is None:
        log.warning(
            "auto_continuation: user %s missing for session %s",
            session.user_id,
            session.id,
        )
        return

    # 1. 更新 tool message 為真結果摘要
    summary = {
        "status": "completed",
        "task_id": report.task_id,
        "report_id": report.id,
        "report_status": report.status.value
        if hasattr(report.status, "value")
        else str(report.status),
        "total_cases": report.total_cases or 0,
        "passed_cases": report.passed_cases or 0,
        "failed_cases": report.failed_cases or 0,
        "duration_ms": report.duration_ms or 0,
        "view_url": f"/#/reports/{report.id}",
    }
    msg.content = json.dumps(summary, ensure_ascii=False)
    await db.flush()

    # 2. 跑 follow-up chat 讓 LLM 給使用者總結
    # 延遲 import 避免 startup 階段 circular(agent_service → app.llm → app.config)
    from app.services import agent_service

    try:
        await agent_service._run_chat_loop(db, session, user)
    except Exception:  # noqa: BLE001
        log.exception(
            "auto_continuation follow-up chat failed for task=%s",
            report.task_id,
        )

    await db.commit()


async def tick() -> int:
    """跑一輪掃描 + 處理。回實際處理的訊息數,方便 log / metric。"""
    async with AsyncSessionLocal() as db:
        stmt = (
            select(AgentMessage, ExecutionReport)
            .join(
                ExecutionReport,
                AgentMessage.task_id == ExecutionReport.task_id,
            )
            .where(AgentMessage.task_id.isnot(None))
            .where(AgentMessage.role == "tool")
            .where(
                ExecutionReport.status.in_(
                    [ReportStatus.PASSED, ReportStatus.FAILED]
                )
            )
            .limit(50)  # 防爆量
        )
        rows = (await db.execute(stmt)).all()

    processed = 0
    for msg, report in rows:
        if await _is_processed(msg.id):
            continue
        # 每筆獨立 transaction,避免一筆失敗影響其他
        try:
            async with AsyncSessionLocal() as db_each:
                msg_attached = await db_each.get(AgentMessage, msg.id)
                report_attached = await db_each.get(ExecutionReport, report.id)
                if msg_attached is None or report_attached is None:
                    continue
                await _process_completion(db_each, msg_attached, report_attached)
            await _mark_processed(msg.id)
            processed += 1
        except Exception:  # noqa: BLE001
            log.exception(
                "auto_continuation process_completion crashed for msg %s",
                msg.id,
            )
    return processed


async def listen_loop(interval_sec: int = DEFAULT_POLL_INTERVAL_SEC) -> None:
    """在 FastAPI lifespan 內由 ``asyncio.create_task()`` 啟動。

    無限迴圈;cancel() 時 asyncio.sleep 會被打斷 → 退出。
    """
    log.info(
        "auto_continuation listener started (interval=%ds)", interval_sec
    )
    try:
        while True:
            try:
                processed = await tick()
                if processed > 0:
                    log.info("auto_continuation processed %d task(s)", processed)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("auto_continuation tick crashed")
            await asyncio.sleep(interval_sec)
    except asyncio.CancelledError:
        log.info("auto_continuation listener cancelled")
        raise
