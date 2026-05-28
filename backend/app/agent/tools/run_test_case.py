"""run_test_case tool — 第一個**非同步** RL tool。

重用既有 [POST /api/executions/run](app/routers/executions.py) 的 dispatch 邏輯:
``collect_execution_plan`` → ``create_report`` → ``celery_app.send_task``。

設計:
* ``is_async = True``:tool 派 Celery 後立刻回 ``status=queued + task_id``,不等
  真結果(robot 可能跑幾分鐘);LLM 自然會給使用者「已排程」回覆。
* ``casbin_permission = P.TESTCASE_EXECUTE``:沒這個權限的 user 連 toolspec 都
  看不到(filter_tools_for_user 在 send_message 前過濾)。
* ``requires_confirmation = False``(暫定):Phase 1c-2 接前端 UI confirm flow
  時改 True,UI 跳「確定要跑這 N 個 case 嗎?」modal。Phase 1c-1 為了能直接
  demo,先不擋。
* tool 內部要 ``db.commit()`` 把 ExecutionReport 落地,worker 才看得到。
  agent_service.send_message 用同一個 db session,提早 commit 是 ok 的
  (Phase 1b 之前的 user message / tool_use assistant message 也會一起 commit,
  本來就會留在 DB)。
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from fastapi import HTTPException

from app.agent.tools.base import Tool, ToolContext, ToolResult
from app.auth.permissions_catalog import P
from app.services.execution_plan_service import collect_execution_plan
from app.services.execution_service import create_report

log = logging.getLogger(__name__)


class RunTestCaseTool(Tool):
    name = "run_test_case"
    description = (
        "在 RL 平台上排程執行測試案例。輸入 node_ids(可多選的 testcase 節點 ID)。"
        " 此 tool 為**非同步**:派出後立刻回 task_id 與 report_id,真實結果由"
        " worker 在背景跑;LLM 應該回應使用者「已排程」並告知 task_id,使用者可"
        " 用 query_recent_reports 或前端追蹤進度。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "node_ids": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "description": "Testcase 或目錄節點 ID 陣列;會自動展開到 leaf case",
            },
            "execution_mode": {
                "type": "string",
                "enum": ["docker", "local"],
                "description": "docker = 後端 spawn 容器(預設);local = 留給本機 agent 認領",
            },
            "ddt_expand": {
                "type": "boolean",
                "description": "DDT 案例是否依序展開每一列;預設 false",
            },
            "enable_recording": {
                "type": "boolean",
                "description": "是否啟用 trace.zip 與 video 收集;預設 true",
            },
        },
        "required": ["node_ids"],
        "additionalProperties": False,
    }
    casbin_permission = P.TESTCASE_EXECUTE
    # Phase 1c-2:requires_confirmation 打開。dispatcher 看到 True 會寫 PendingAction
    # + placeholder tool message,等使用者按 approve / reject 才真執行。
    # destructive action 紅線(會跑真實測試、占用容器、可能影響資料)必須有 confirm。
    requires_confirmation = True
    is_async = True
    # Per-user 上限 3:LLM 在 loop 內派完第 4 個會被擋,看到 fail message 收手。
    # TTL 30 分鐘(預設)— 涵蓋 robot 一次跑的合理時長;Celery worker 完成後
    # 由 Phase 1c-2 的事件回流機制 release(目前先靠 TTL 自然到期)。
    concurrency_limit_per_user = 3

    async def execute(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        raw_ids = list(kwargs.get("node_ids") or [])
        if not raw_ids:
            return ToolResult.fail(
                "node_ids 不可為空",
                llm_visible="參數錯誤:必須提供至少一個 node_id。",
            )
        # 去重,保留順序
        raw_ids = list(dict.fromkeys(raw_ids))

        execution_mode = (kwargs.get("execution_mode") or "docker").lower()
        ddt_expand = bool(kwargs.get("ddt_expand", False))
        enable_recording = bool(kwargs.get("enable_recording", True))

        # 1) 展開 execution plan(收集 setup + main)
        try:
            plan = await collect_execution_plan(
                ctx.db, node_ids=raw_ids, user=ctx.user
            )
        except HTTPException as e:
            # collect_execution_plan 用 HTTPException 表達 user 輸入錯;轉成 LLM 看得懂的訊息
            return ToolResult.fail(
                f"execution_plan_failed: {e.detail}",
                llm_visible=f"無法展開測試計畫:{e.detail}",
            )

        setup_ids = plan["setup_testcase_ids"]
        main_ids = plan["main_testcase_ids"]
        project_id = plan["project_id"]
        total = len(setup_ids) + len(main_ids)

        if total == 0:
            return ToolResult.fail(
                "no_testcases_found",
                llm_visible="提供的 node_ids 展開後沒有可執行的 testcase。",
            )

        task_id = str(uuid.uuid4())
        source_node_id = raw_ids[0]
        multi_source = raw_ids if len(raw_ids) > 1 else None

        # 2) 建 ExecutionReport(總筆數已知)
        report = await create_report(
            ctx.db,
            project_id,
            "Agent",  # trigger_type 寫成 "Agent" 與手動 / 排程區分
            total,
            task_id,
            execution_mode=execution_mode,
            source_node_id=source_node_id,
            source_node_ids=multi_source,
            ddt_expand=ddt_expand,
            enable_recording=enable_recording,
        )

        # 3) commit — Celery worker 在另一個 process,要先 commit 才看得到
        # (沿用 executions.py 第 79 行同樣模式)
        await ctx.db.commit()

        # 4) 派 Celery;local mode 不派,留給本機 agent 認領
        celery_status = "queued"
        celery_error: str | None = None
        if execution_mode == "docker":
            try:
                from tasks.celery_app import celery_app

                celery_app.send_task(
                    "tasks.execution_tasks.run_tests",
                    kwargs={
                        "task_id": task_id,
                        "report_id": report.id,
                        "testcase_ids": setup_ids + main_ids,
                        "setup_testcase_ids": setup_ids,
                        "ddt_expand": ddt_expand,
                        "enable_recording": enable_recording,
                    },
                )
            except Exception as e:  # noqa: BLE001 - Celery 斷線仍要回 LLM 一個說法
                log.warning("celery send_task failed for agent tool: %s", e)
                celery_status = "celery_unreachable"
                celery_error = (
                    f"{type(e).__name__}: {e}. "
                    "報告已建立但 worker 派發失敗;請檢查 Celery/Valkey 狀態。"
                )
        else:
            celery_status = "awaiting_local_agent"

        payload = {
            "status": celery_status,
            "task_id": task_id,
            "report_id": report.id,
            "project_id": project_id,
            "total_cases": total,
            "setup_cases": len(setup_ids),
            "main_cases": len(main_ids),
            "execution_mode": execution_mode,
            "status_url": f"/api/executions/{task_id}/status",
            "logs_ws_url": f"/ws/v1/executions/{task_id}/logs",
        }
        if celery_error:
            payload["error"] = celery_error

        return ToolResult.ok(
            json.dumps(payload, ensure_ascii=False),
            task_id=task_id,
            report_id=report.id,
            status=celery_status,
        )
