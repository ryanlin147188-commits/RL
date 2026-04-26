import asyncio
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.database import init_db
from app.routers import projects, tree_nodes, testcases, executions, reports, upload, import_export, recordings, schedules, local_runner, test_rounds, project_settings, screenshot_baselines, system, defects, test_milestones, test_plans, requirements, test_data_sets, test_documents, wbs_items, settings as app_settings, todos
# 確保 13 個新增 model 在 init_db() 前已 import 註冊到 Base.metadata
from app.models import (  # noqa: F401
    Defect, TestMilestone, TestPlan, Requirement, RequirementTestcaseLink,
    TestDataSet, TestDocument, WbsItem,
    Role, NotificationPreference, EmailConfig, AiTokenConfig, TodoItem,
)
from app.services.schedule_service import scheduler_loop


async def _seed_default_roles() -> None:
    """確保 3 個系統內建角色（Admin / QA / Viewer）存在；不存在才建立。"""
    from sqlalchemy import select
    from app.database import AsyncSessionLocal

    DEFAULTS = [
        {
            "name": "Admin",
            "description": "系統管理員 — 全部權限",
            "permissions_json": [
                "project.read", "project.write", "project.delete",
                "testcase.read", "testcase.write", "testcase.delete", "testcase.execute",
                "defect.read", "defect.write", "defect.delete",
                "requirement.read", "requirement.write", "requirement.delete",
                "plan.read", "plan.write", "plan.approve",
                "wbs.read", "wbs.write",
                "document.read", "document.write",
                "report.read",
                "settings.read", "settings.write",
                "user.manage", "role.manage",
            ],
        },
        {
            "name": "QA",
            "description": "測試人員 — 撰寫 / 執行測試 + 缺陷管理",
            "permissions_json": [
                "project.read",
                "testcase.read", "testcase.write", "testcase.execute",
                "defect.read", "defect.write",
                "requirement.read",
                "plan.read", "plan.write",
                "wbs.read",
                "document.read", "document.write",
                "report.read",
                "settings.read",
            ],
        },
        {
            "name": "Viewer",
            "description": "檢視者 — 只讀全部",
            "permissions_json": [
                "project.read", "testcase.read", "defect.read",
                "requirement.read", "plan.read", "wbs.read",
                "document.read", "report.read", "settings.read",
            ],
        },
    ]

    async with AsyncSessionLocal() as session:
        for spec in DEFAULTS:
            existing = (
                await session.execute(select(Role).where(Role.name == spec["name"]))
            ).scalar_one_or_none()
            if existing is None:
                session.add(Role(
                    name=spec["name"],
                    description=spec["description"],
                    permissions_json=spec["permissions_json"],
                    is_system=True,
                ))
        await session.commit()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup：建立 PIC 資料夾 + 自動建表 + 啟動排程背景任務
    os.makedirs(settings.PIC_FOLDER, exist_ok=True)
    await init_db()
    try:
        await _seed_default_roles()
    except Exception as e:  # 不要因為 seed 失敗而擋住服務啟動
        import logging
        logging.getLogger(__name__).warning("seed default roles failed: %s", e)
    scheduler_task = asyncio.create_task(scheduler_loop())
    try:
        yield
    finally:
        # Shutdown：停掉排程背景任務
        scheduler_task.cancel()
        try:
            await scheduler_task
        except (asyncio.CancelledError, Exception):
            pass


app = FastAPI(
    title="AutoTest v1.0 API",
    description="企業級自動化測試平台後端 API",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 提供靜態截圖檔案
app.mount("/pics", StaticFiles(directory=settings.PIC_FOLDER), name="pics")

# ── 路由註冊（REST 端點掛 /api，WebSocket 掛 /ws）──
app.include_router(projects.router,        prefix="/api", tags=["A · 專案與樹"])
app.include_router(tree_nodes.router,      prefix="/api", tags=["A · 專案與樹"])
app.include_router(testcases.router,       prefix="/api", tags=["B · 測試案例編輯"])
app.include_router(import_export.router,   prefix="/api", tags=["B · 測試案例編輯"])
app.include_router(executions.rest_router, prefix="/api", tags=["C · 執行引擎"])
app.include_router(executions.ws_router,   prefix="/ws",  tags=["C · 執行引擎 WebSocket"])
app.include_router(reports.router,         prefix="/api", tags=["D · 報告與儀表板"])
app.include_router(upload.router,          prefix="/api", tags=["D · 報告與儀表板"])
app.include_router(recordings.router,      prefix="/api", tags=["E · 錄製"])
app.include_router(schedules.router,       prefix="/api", tags=["F · 排程"])
app.include_router(local_runner.router,    prefix="/api", tags=["G · 本機執行"])
app.include_router(test_rounds.router,     prefix="/api", tags=["H · 測試回合"])
app.include_router(project_settings.router, prefix="/api", tags=["I · 專案設定（環境變數 / 設備）"])
app.include_router(screenshot_baselines.router, prefix="/api", tags=["J · Screenshot Diff Baseline"])
app.include_router(system.router,          prefix="/api", tags=["K · 系統狀態"])
app.include_router(defects.router,         prefix="/api", tags=["L · 缺陷管理"])
app.include_router(test_milestones.router, prefix="/api", tags=["M · 測試時程"])
app.include_router(test_plans.router,      prefix="/api", tags=["N · 測試計畫"])
app.include_router(requirements.router,    prefix="/api", tags=["O · 需求 / RTM"])
app.include_router(test_data_sets.router,  prefix="/api", tags=["P · 測試資料集 (DDT)"])
app.include_router(test_documents.router,  prefix="/api", tags=["Q · 測試文件"])
app.include_router(wbs_items.router,       prefix="/api", tags=["R · WBS"])
app.include_router(app_settings.router,    prefix="/api", tags=["S · 設定"])
app.include_router(todos.router,           prefix="/api", tags=["T · 待辦"])


@app.get("/", tags=["Health"])
async def root():
    return {"status": "ok", "service": "AutoTest v1.0 API"}
