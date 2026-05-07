"""本機執行 Agent（local-runner）API。

使用流程：
    1. 使用者在前端把「環境切換」按鈕切到「本機」
    2. 按「執行測試」 → backend 建立 `execution_mode=local` 的報告，不送 Celery
    3. 使用者在本機跑 `python local_agent.py --server http://<ip>`
    4. Agent 定期 POST /api/local-runner/claim；backend atomically 認領一筆未被接手的
       local 報告，回傳每個 testcase 的 steps_json + ddt_json
    5. Agent 用 Playwright 執行，完成後 POST /api/local-runner/tasks/{task_id}/complete

Endpoints:
    - POST /api/local-runner/claim
    - POST /api/local-runner/tasks/{task_id}/complete
    - GET  /api/local-runner/agent         （下載 agent Python 腳本）
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.tenant import TenantQuery
from app.config import settings
from app.database import get_db
from app.models.execution_report import ExecutionReport, ReportStatus
from app.models.testcase_content import TestcaseContent
from app.services.artifact_urls import sign_artifact_url
from app.services.execution_service import collect_testcase_ids


def _publish_ws(task_id: str, message: dict[str, Any]) -> None:
    """Best-effort 推一則訊息到 WS log channel（給 Test Execution Console 即時顯示用）。"""
    try:
        import redis as _redis

        r = _redis.from_url(settings.REDIS_URL)
        r.publish(f"task:{task_id}:logs", json.dumps(message))
        r.close()
    except Exception:
        pass

router = APIRouter()


class ClaimRequest(BaseModel):
    agent_id: str = Field(..., description="Agent 執行個體識別，任意字串")


class CaseJob(BaseModel):
    testcase_id: str
    name: Optional[str] = None
    steps_json: list[dict[str, Any]] = Field(default_factory=list)
    ddt_json: Optional[dict[str, Any]] = None
    # 是否為前置案例(在 main 之前先跑;失敗會讓 main 全部 SKIP)
    is_setup: bool = False


class ClaimResponse(BaseModel):
    report_id: str
    task_id: str
    cases: list[CaseJob]
    # 專案環境變數 — agent 端用來把 ${VAR} 展開到 step 欄位
    # (DDT row 同名值優先,跟 docker runner 規則一致)
    project_env_vars: dict[str, str] = Field(default_factory=dict)
    # 前置案例 id 清單(同時也會出現在 cases 內並 is_setup=True)
    # 任一失敗,agent 必須把後續 main 案例全標 FAILED + skip
    setup_testcase_ids: list[str] = Field(default_factory=list)


class StepLogInput(BaseModel):
    testcase_node_id: Optional[str] = None
    step_index: int = 0
    status: str = "PASSED"
    duration_ms: int = 0
    error_message: Optional[str] = None
    pre_screenshot_url: Optional[str] = None
    post_screenshot_url: Optional[str] = None


class CompleteRequest(BaseModel):
    status: str = Field("PASSED", description="PASSED / FAILED")
    passed_cases: int = 0
    failed_cases: int = 0
    duration_ms: int = 0
    error_message: Optional[str] = None
    # Agent 端收集的每步結果；backend 會寫進 ExecutionStepLog 供詳細報告顯示
    steps: list[StepLogInput] = Field(default_factory=list)


@router.post("/local-runner/claim", tags=["G · 本機執行"])
async def claim_local_job(
    payload: ClaimRequest = Body(default=ClaimRequest(agent_id="unknown")),
    db: AsyncSession = Depends(get_db),
):
    """搶鎖取一筆未被認領的 local 模式報告。無可認領時回 204。"""
    # 只撈 status=RUNNING 且 execution_mode=local 且 claimed_at=NULL
    result = await db.execute(
        TenantQuery.for_(ExecutionReport)
        .where(
            ExecutionReport.execution_mode == "local",
            ExecutionReport.status == ReportStatus.RUNNING,
            ExecutionReport.claimed_at.is_(None),
        )
        .order_by(ExecutionReport.created_at.asc())
        .limit(1)
    )
    report = result.scalar_one_or_none()
    if report is None:
        # 204 No Content：Agent 再等等就好
        from fastapi import Response
        return Response(status_code=204)

    # Atomic 搶鎖：只在 claimed_at 仍為 NULL 時寫入；若另一個 agent 已先搶走，rowcount=0
    now = datetime.now()
    stmt = (
        update(ExecutionReport)
        .where(ExecutionReport.id == report.id, ExecutionReport.claimed_at.is_(None))
        .values(claimed_at=now)
    )
    upd = await db.execute(stmt)
    if upd.rowcount == 0:
        from fastapi import Response
        return Response(status_code=204)

    # 還原 testcase_ids — 用 execution_plan_service 展開,讓 setup / main 一致
    # 多選(node_ids):優先讀 source_node_ids;否則退回單選 source_node_id
    raw_node_ids: list[str] = list(report.source_node_ids or [])
    if not raw_node_ids and report.source_node_id:
        raw_node_ids = [report.source_node_id]
    if not raw_node_ids:
        raise HTTPException(status_code=500, detail="report 缺少 source_node_id(s),無法還原測試案例")
    from app.services.execution_plan_service import _expand_inputs, _gather_preconditions

    main_ids = await _expand_inputs(db, raw_node_ids)
    if not main_ids:
        report.status = ReportStatus.FAILED
        await db.flush()
        raise HTTPException(status_code=404, detail="此 report 無可執行的測試案例")
    setup_ids: list[str] = []
    try:
        pre_ids = await _gather_preconditions(db, main_ids)
        main_set = set(main_ids)
        setup_ids = [tid for tid in pre_ids if tid not in main_set]
    except HTTPException:
        # 循環前置:讓 agent 看到任務但 setup 為空,主案例仍照跑
        # (cycle 會在 docker / api 觸發時就被擋,正常路徑跑不到這)
        setup_ids = []

    testcase_ids = setup_ids + main_ids

    # 為每個 testcase 抓 steps_json + ddt_json
    # 若 report.ddt_expand=False → 把 DDT 的 rows 截到只有第一列(整個 testcase 只跑一次)
    # TestcaseContent 的 primary key 是 node_id(不是 id)
    cases: list[CaseJob] = []
    rows_db = await db.execute(
        TenantQuery.for_(TestcaseContent).where(TestcaseContent.node_id.in_(testcase_ids))
    )
    tc_map = {t.node_id: t for t in rows_db.scalars()}
    setup_set = set(setup_ids)
    for tid in testcase_ids:
        tc = tc_map.get(tid)
        ddt = (tc.ddt_json if tc else None) or {}
        if isinstance(ddt, dict) and not report.ddt_expand:
            rs = ddt.get("rows") or []
            if len(rs) > 1:
                ddt = {"headers": ddt.get("headers") or [], "rows": rs[:1]}
        cases.append(
            CaseJob(
                testcase_id=tid,
                name=None,
                steps_json=(tc.steps_json if tc and tc.steps_json else []),
                ddt_json=ddt if ddt else None,
                is_setup=tid in setup_set,
            )
        )

    # 撈 project_env_vars(同 docker runner 邏輯,讓 agent 端做 ${VAR} 展開)
    from app.models.project_env_var import ProjectEnvVar

    env_rows = (
        await db.execute(
            TenantQuery.for_(ProjectEnvVar).where(
                ProjectEnvVar.project_id == report.project_id
            )
        )
    ).scalars().all()
    env_map = {row.name: row.value for row in env_rows}

    # 通知前端 Console:已被某個 agent 認領
    _publish_ws(
        report.task_id or report.id,
        {
            "type": "log",
            "level": "INFO",
            "message": f"🖥️  本機 Agent『{payload.agent_id}』已認領此任務,開始執行…",
        },
    )

    return ClaimResponse(
        report_id=report.id,
        task_id=report.task_id or report.id,
        cases=cases,
        project_env_vars=env_map,
        setup_testcase_ids=setup_ids,
    )


@router.post("/local-runner/tasks/{task_id}/complete", tags=["G · 本機執行"])
async def complete_local_job(
    task_id: str,
    payload: CompleteRequest,
    db: AsyncSession = Depends(get_db),
):
    """Agent 執行完後回報最終結果 + 每步記錄（寫進 ExecutionStepLog 供詳細報告顯示）。"""
    import uuid as _uuid
    from app.models.execution_step_log import ExecutionStepLog, StepStatus

    result = await db.execute(
        TenantQuery.for_(ExecutionReport).where(ExecutionReport.task_id == task_id)
    )
    report = result.scalar_one_or_none()
    if report is None:
        raise HTTPException(status_code=404, detail="找不到對應的 report")

    status_upper = (payload.status or "FAILED").upper()
    if status_upper == "PASSED":
        report.status = ReportStatus.PASSED
    else:
        report.status = ReportStatus.FAILED
    report.passed_cases = int(payload.passed_cases or 0)
    report.failed_cases = int(payload.failed_cases or 0)
    report.duration_ms = int(payload.duration_ms or 0)

    # 寫入每步記錄（若 agent 有提供）
    for s in payload.steps or []:
        st_raw = (s.status or "FAILED").upper()
        try:
            st_enum = StepStatus(st_raw)
        except (KeyError, ValueError):
            st_enum = StepStatus.FAILED
        db.add(
            ExecutionStepLog(
                id=str(_uuid.uuid4()),
                report_id=report.id,
                testcase_node_id=s.testcase_node_id,
                step_index=int(s.step_index or 0),
                status=st_enum,
                duration_ms=int(s.duration_ms or 0),
                error_message=s.error_message,
                pre_screenshot_url=s.pre_screenshot_url,
                post_screenshot_url=s.post_screenshot_url,
            )
        )

    await db.flush()

    # 通知 Test Execution Console：任務結束；WS 端會讀到 type=done 後自動收尾
    _publish_ws(
        task_id,
        {
            "type": "log",
            "level": "INFO",
            "message": (
                f"🏁 本機 Agent 回報完成：passed={report.passed_cases} "
                f"failed={report.failed_cases} duration={report.duration_ms}ms"
            ),
        },
    )
    _publish_ws(task_id, {"type": "done", "status": report.status.value})

    return {"ok": True, "report_id": report.id, "status": report.status.value}


_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


@router.post("/local-runner/upload-screenshot", tags=["G · 本機執行"])
async def upload_screenshot(
    report_id: str = Form(...),
    filename: str = Form(...),
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
):
    """Agent 上傳單張截圖;透過 storage_service 寫入 SeaweedFS(或 local fallback)。

    URL 格式為 relative `/pics/<report_id>/<filename>`,與 Docker runner 一致。
    """
    # 驗證 report 存在(避免任意寫入)
    report = await db.get(ExecutionReport, report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="report_id 不存在")

    # 檔名防穿越:只保留英數與 . _ -
    safe_name = _SAFE_FILENAME_RE.sub("_", filename.strip())
    if not safe_name or safe_name in (".", ".."):
        raise HTTPException(status_code=400, detail="filename 不合法")
    if "." not in safe_name:
        safe_name += ".png"

    content = await file.read()
    if len(content) > 10 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="截圖超過 10 MB 上限")

    # 寫到 SeaweedFS(STORAGE_BACKEND=s3)
    from app.services.storage_service import save_bytes
    key = f"{report_id}/{safe_name}"
    content_type = file.content_type or "image/png"
    url = save_bytes(content, key, bucket="pic", content_type=content_type)
    return {"ok": True, "url": sign_artifact_url(url), "size": len(content)}


@router.get("/local-runner/agent", response_class=PlainTextResponse, tags=["G · 本機執行"])
async def download_agent_script():
    """提供 agent 腳本供使用者下載；內容從同目錄的 local_agent_template.py 讀出。"""
    template_path = Path(__file__).resolve().parent.parent / "static" / "local_agent.py"
    if not template_path.exists():
        raise HTTPException(status_code=500, detail="local_agent.py 未部署")
    return template_path.read_text(encoding="utf-8")
