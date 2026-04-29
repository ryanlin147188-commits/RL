import asyncio
import json
import uuid

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from sqlalchemy import select
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
    """
    觸發測試執行:
    1. 收集目標節點下所有 TESTCASE
    2. 建立 execution_reports 紀錄
    3. 丟入 Celery 背景佇列(非同步執行)
    """
    node = await db.get(TreeNode, payload.node_id)
    await ensure_project_in_scope(
        db, node.project_id if node else None, user, not_found_detail="Node not found"
    )

    testcase_ids = await collect_testcase_ids(db, payload.node_id)
    if not testcase_ids:
        raise HTTPException(
            status_code=400,
            detail="No TESTCASE nodes found under the given node",
        )

    task_id = str(uuid.uuid4())
    report = await create_report(
        db, node.project_id, payload.trigger_type, len(testcase_ids), task_id,
        execution_mode=payload.execution_mode,
        source_node_id=payload.node_id,
        ddt_expand=payload.ddt_expand,
        enable_recording=payload.enable_recording,
    )

    # local 模式：不送 Celery，留給本機 agent 透過 /api/local-runner/claim 認領
    if (payload.execution_mode or "docker").lower() == "local":
        return ExecutionRunResponse(
            task_id=task_id,
            report_id=report.id,
            message=(
                f"Local execution queued for {len(testcase_ids)} test case(s). "
                "請確認本機 agent 已啟動（python local_agent.py）"
            ),
        )

    try:
        from tasks.celery_app import celery_app
        celery_app.send_task(
            "tasks.execution_tasks.run_tests",
            kwargs={
                "task_id": task_id,
                "report_id": report.id,
                "testcase_ids": testcase_ids,
                "ddt_expand": bool(payload.ddt_expand),
                "enable_recording": bool(payload.enable_recording),
            },
        )
    except Exception:
        # Celery / Redis 未啟動時不阻擋 API 回應（開發期友善提示）
        pass

    return ExecutionRunResponse(
        task_id=task_id,
        report_id=report.id,
        message=(
            f"Execution started for {len(testcase_ids)} test case(s). "
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

    # 2. 更新 report 狀態
    if report.status.value == "RUNNING":
        from app.models.execution_report import ReportStatus

        report.status = ReportStatus.FAILED
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
        r.publish(f"task:{task_id}:logs", _json.dumps({"type": "done", "status": "CANCELLED"}))
        r.close()
    except Exception:
        pass

    return {"ok": True, "task_id": task_id, "status": "FAILED"}


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
