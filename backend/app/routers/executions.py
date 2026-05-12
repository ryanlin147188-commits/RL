import asyncio
import json
import uuid

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.auth.scope import ensure_project_in_scope
from app.config import settings
from app.database import get_db
from app.models.execution_report import ExecutionReport
from app.models.tree_node import TreeNode
from app.models.user import User
from app.schemas.execution_report import (
    ExecutionRunRequest,
    ExecutionRunResponse,
    ExecutionStatusResponse,
)
from app.services.execution_plan_service import collect_execution_plan
from app.services.execution_service import collect_testcase_ids, create_report

rest_router = APIRouter()
ws_router = APIRouter()


# API 9. POST /api/v1/executions
@rest_router.post("/executions", response_model=ExecutionRunResponse, status_code=201)
async def run_execution(
    payload: ExecutionRunRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """觸發測試執行:
      1. 解 payload:`node_ids`(多選)優先;無則退回舊 `node_id`
      2. 用 collect_execution_plan 展開為 setup + main(含前置案例去重 / cycle 偵測)
      3. 建立 execution_reports 紀錄(total_cases = setup + main)
      4. 丟入 Celery 背景佇列;local mode 留給本機 agent 認領
    """
    raw_ids: list[str] = []
    if payload.node_ids:
        raw_ids = list(dict.fromkeys(payload.node_ids))
    elif payload.node_id:
        raw_ids = [payload.node_id]
    if not raw_ids:
        raise HTTPException(status_code=422, detail="node_id 或 node_ids 至少擇一")

    plan = await collect_execution_plan(db, node_ids=raw_ids, user=user)
    setup_ids: list[str] = plan["setup_testcase_ids"]
    main_ids: list[str] = plan["main_testcase_ids"]
    project_id: str = plan["project_id"]

    total = len(setup_ids) + len(main_ids)
    task_id = str(uuid.uuid4())
    # source_node_id:沿用第一個 input 當作報告的「來源節點」(報告頁需要)
    source_node_id = raw_ids[0]
    # source_node_ids:多選時保存完整清單,讓 local agent 認領時可重展開
    multi_source = raw_ids if len(raw_ids) > 1 else None
    report = await create_report(
        db,
        project_id,
        payload.trigger_type,
        total,
        task_id,
        execution_mode=payload.execution_mode,
        source_node_id=source_node_id,
        source_node_ids=multi_source,
        ddt_expand=payload.ddt_expand,
        enable_recording=payload.enable_recording,
    )

    if (payload.execution_mode or "docker").lower() == "local":
        return ExecutionRunResponse(
            task_id=task_id,
            report_id=report.id,
            message=(
                f"Local execution queued for {total} test case(s) "
                f"(setup={len(setup_ids)}, main={len(main_ids)}). "
                "請確認本機 agent 已啟動(python local_agent.py)"
            ),
        )

    try:
        from tasks.celery_app import celery_app
        celery_app.send_task(
            "tasks.execution_tasks.run_tests",
            kwargs={
                "task_id": task_id,
                "report_id": report.id,
                # 合併送進 worker;worker 內讀回 setup_ids 知道哪些要先跑
                "testcase_ids": setup_ids + main_ids,
                "setup_testcase_ids": setup_ids,
                "ddt_expand": bool(payload.ddt_expand),
                "enable_recording": bool(payload.enable_recording),
            },
        )
    except Exception:
        # Celery / Redis 未啟動時不阻擋 API 回應(開發期友善提示)
        pass

    return ExecutionRunResponse(
        task_id=task_id,
        report_id=report.id,
        message=(
            f"Execution started for {total} test case(s) "
            f"(setup={len(setup_ids)}, main={len(main_ids)}). "
            f"Connect to WS /ws/v1/executions/{task_id}/logs for live logs."
        ),
    )


# API 10. GET /api/v1/executions/{task_id}/status
@rest_router.get("/executions/{task_id}/status", response_model=ExecutionStatusResponse)
async def get_execution_status(
    task_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    輪詢執行進度。即時性要求請改用 WS /ws/v1/executions/{task_id}/logs。
    """
    result = await db.execute(
        select(ExecutionReport).where(ExecutionReport.task_id == task_id)
    )
    report = result.scalar_one_or_none()
    await ensure_project_in_scope(
        db, report.project_id if report else None, user, not_found_detail="Task not found"
    )

    completed = report.passed_cases + report.failed_cases
    progress = round(completed / report.total_cases, 4) if report.total_cases > 0 else 0.0

    return ExecutionStatusResponse(
        task_id=task_id,
        report_id=report.id,
        status=report.status,
        total_cases=report.total_cases,
        passed_cases=report.passed_cases,
        failed_cases=report.failed_cases,
        progress=progress,
    )


# POST /api/executions/{task_id}/cancel  ── 中斷執行中的任務
@rest_router.post("/executions/{task_id}/cancel", status_code=200)
async def cancel_execution(
    task_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    取消執行中的任務：
      1. 呼叫 Celery revoke(terminate=True, signal='SIGTERM')
         - signal 會傳遞到 worker 行程；worker 若正在執行 subprocess.run(robot ...)
           會讓 robot 收到 SIGTERM 而結束
      2. 把 execution_reports 狀態改成 FAILED，讓前端清單立刻離開 RUNNING
    """
    result = await db.execute(
        select(ExecutionReport).where(ExecutionReport.task_id == task_id)
    )
    report = result.scalar_one_or_none()
    await ensure_project_in_scope(
        db, report.project_id if report else None, user, not_found_detail="Task not found"
    )

    # 1. 送 Celery revoke
    try:
        from tasks.celery_app import celery_app

        celery_app.control.revoke(task_id, terminate=True, signal="SIGTERM")
    except Exception:
        # Celery 無法連線也不要擋 API；至少先把 DB 狀態修好
        pass

    # 1b. 砍掉這個 task 派生的 robot-runner 容器(SIGTERM 殺 celery worker
    # 不會自動把 docker run 出來的容器也帶走 → 否則就會變成孤兒容器繼續跑、
    # 而 step log 一筆都沒寫進 DB)。容器命名規則:robot-<task[:8]>-<case[:8]>。
    killed_containers: list[str] = []
    try:
        import docker  # type: ignore

        dclient = docker.from_env()
        prefix = f"robot-{task_id[:8]}-"
        for c in dclient.containers.list():
            if c.name.startswith(prefix):
                try:
                    c.kill()
                    try:
                        c.remove(force=True)
                    except Exception:
                        pass
                    killed_containers.append(c.name)
                except Exception:
                    pass
    except Exception:
        pass

    # 2. 更新 report 狀態
    if report.status.value == "RUNNING":
        from app.models.execution_report import ReportStatus

        report.status = ReportStatus.FAILED
        await db.flush()

    # 2b. 若 report 目前一條 step log 都沒有,寫一條 synthetic FAILED 進去
    # —— 避免使用者打開取消過的報告看到一片空白。
    from app.models.execution_step_log import ExecutionStepLog, StepStatus
    existing = (await db.execute(
        select(ExecutionStepLog).where(
            ExecutionStepLog.report_id == report.id
        ).limit(1)
    )).scalar_one_or_none()
    if existing is None:
        import uuid as _uuid

        await db.execute(
            insert(ExecutionStepLog).values(
                id=str(_uuid.uuid4()),
                report_id=report.id,
                testcase_node_id=report.source_node_id,
                step_index=0,
                status=StepStatus.FAILED,
                duration_ms=0,
                error_message=(
                    "🛑 使用者取消執行 — 任務尚未跑完任何步驟就被中止"
                    + (f"(已強制中止 {len(killed_containers)} 個 runner 容器)" if killed_containers else "")
                ),
            )
        )
        await db.flush()

    # 3. 額外送一個「cancelled」訊息到 WS log channel，讓前端可以看到
    try:
        import json as _json

        import redis as _redis

        r = _redis.from_url(settings.REDIS_URL)
        r.publish(
            f"task:{task_id}:logs",
            _json.dumps({"type": "log", "level": "WARN", "message": "🛑 使用者取消執行"}),
        )
        if killed_containers:
            r.publish(
                f"task:{task_id}:logs",
                _json.dumps({"type": "log", "level": "WARN", "message": f"已強制中止 {len(killed_containers)} 個 runner 容器"}),
            )
        r.publish(f"task:{task_id}:logs", _json.dumps({"type": "done", "status": "CANCELLED"}))
        r.close()
    except Exception:
        pass

    return {"ok": True, "task_id": task_id, "status": "FAILED", "killed_runners": killed_containers}


# WS 11. WS /ws/v1/executions/{task_id}/logs  （掛在 /ws/v1 prefix 下）
@ws_router.websocket("/executions/{task_id}/logs")
async def websocket_logs(task_id: str, websocket: WebSocket):
    """
    透過 Redis pub/sub 即時串流執行 Log 到前端終端機。
    Celery Worker 發布至頻道 task:{task_id}:logs，
    此 endpoint 訂閱後轉發給 WebSocket 客戶端。
    """
    await websocket.accept()
    channel = f"task:{task_id}:logs"

    try:
        import redis.asyncio as aioredis  # type: ignore[import]

        r = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
        pubsub = r.pubsub()
        await pubsub.subscribe(channel)

        done = asyncio.Event()

        async def redis_to_ws() -> None:
            """訂閱 Redis，收到訊息就轉送給 WebSocket。"""
            async for message in pubsub.listen():
                if message["type"] != "message":
                    continue
                data: str = message["data"]
                try:
                    await websocket.send_text(data)
                except Exception:
                    break
                payload = json.loads(data)
                if payload.get("type") == "done":
                    done.set()
                    break

        async def ws_heartbeat() -> None:
            """維持連線；前端可定期送 'ping'，回應 pong。"""
            try:
                while not done.is_set():
                    try:
                        data = await asyncio.wait_for(websocket.receive_text(), timeout=1.0)
                        if data == "ping":
                            await websocket.send_json({"type": "pong"})
                    except asyncio.TimeoutError:
                        continue
                    except Exception:
                        break
            finally:
                done.set()

        redis_task = asyncio.create_task(redis_to_ws())
        ws_task = asyncio.create_task(ws_heartbeat())
        await done.wait()
        redis_task.cancel()
        ws_task.cancel()
        await asyncio.gather(redis_task, ws_task, return_exceptions=True)

        await pubsub.unsubscribe(channel)
        await r.aclose()

    except ImportError:
        # redis 套件未安裝時的降級處理
        await websocket.send_json(
            {"type": "error", "message": "Redis not available; install redis package"}
        )
    except WebSocketDisconnect:
        pass
    finally:
        try:
            await websocket.close()
        except Exception:
            pass
