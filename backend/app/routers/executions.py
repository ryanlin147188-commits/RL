import asyncio
import json
import uuid

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models.execution_report import ExecutionReport
from app.models.tree_node import TreeNode
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
    payload: ExecutionRunRequest, db: AsyncSession = Depends(get_db)
):
    """
    觸發測試執行：
    1. 收集目標節點下所有 TESTCASE
    2. 建立 execution_reports 紀錄
    3. 丟入 Celery 背景佇列（非同步執行）
    """
    node = await db.get(TreeNode, payload.node_id)
    if node is None:
        raise HTTPException(status_code=404, detail="Node not found")

    testcase_ids = await collect_testcase_ids(db, payload.node_id)
    if not testcase_ids:
        raise HTTPException(
            status_code=400,
            detail="No TESTCASE nodes found under the given node",
        )

    task_id = str(uuid.uuid4())
    report = await create_report(
        db, node.project_id, payload.trigger_type, len(testcase_ids), task_id
    )

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
async def get_execution_status(task_id: str, db: AsyncSession = Depends(get_db)):
    """
    輪詢執行進度。即時性要求請改用 WS /ws/v1/executions/{task_id}/logs。
    """
    result = await db.execute(
        select(ExecutionReport).where(ExecutionReport.task_id == task_id)
    )
    report = result.scalar_one_or_none()
    if report is None:
        raise HTTPException(status_code=404, detail="Task not found")

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
