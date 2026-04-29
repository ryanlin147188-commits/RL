import asyncio
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.database import init_db
from app.routers import projects, tree_nodes, testcases, executions, reports, upload, import_export, recordings, schedules, local_runner, test_rounds, project_settings, screenshot_baselines, system, defects, test_milestones, test_plans, requirements, test_data_sets, test_documents, wbs_items, settings as app_settings, todos, todo_links, auth, ai, ai_chat, audit_logs, organizations, oidc, notifications, mock_endpoints, db_configs, groups, test_versions, reviews
# 確保新增 model 在 init_db() 前已 import 註冊到 Base.metadata
from app.models import (  # noqa: F401
    Defect, TestMilestone, TestPlan, Requirement, RequirementTestcaseLink,
    TestDataSet, TestDocument, WbsItem,
    Role, NotificationPreference, Notification, EmailConfig, AiTokenConfig, TodoItem, TodoLink, User,
    Organization, AuditLog, OidcProvider,
    MockEndpoint, DbConfig,
    AiConversation, AiMessage,
    Group, GroupMembership,
    OrgInvite,
    TestVersion,
)
from app.middleware import AuthMiddleware
from app.audit import AuditMiddleware
from app.observability import (
    install_metrics,
    install_sentry,
    install_tracing,
    instrument_app,
)
from app.rate_limit import limiter
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
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


async def _seed_default_org_and_backfill() -> None:
    """確保 Default Organization 存在；把所有 organization_id IS NULL 的既有資料掛上去。

    一次性 backfill：適合升級到多租戶版本的舊資料庫。
    """
    from sqlalchemy import select, update, text
    from app.database import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        org = (
            await session.execute(select(Organization).where(Organization.slug == "default"))
        ).scalar_one_or_none()
        if not org:
            org = Organization(
                slug="default",
                name="Default Organization",
                description="自動建立的預設組織；未指定 organization_id 的所有資料會歸屬於此",
                plan="free",
            )
            session.add(org)
            await session.flush()

        # 把現有 NULL 的資料一次補上 default org id
        for tbl in (
            "users", "projects", "roles", "email_configs",
            "ai_token_configs", "todo_items", "audit_logs",
        ):
            try:
                await session.execute(
                    text(f"UPDATE {tbl} SET organization_id = :oid WHERE organization_id IS NULL"),
                    {"oid": org.id},
                )
            except Exception:
                # 表還不存在或欄位還沒 ALTER 上去
                pass
        await session.commit()


async def _warn_if_no_users() -> None:
    """若資料庫沒有任何使用者,在啟動 log 印出建立 admin 的指引。

    過去版本會自動建立 admin/admin123,但該預設密碼在 codebase 公開後等同無認證。
    現改為「啟動時偵測 + 提示使用者執行 CLI」,避免在公開部署環境留下預設帳號。
    """
    import logging
    from sqlalchemy import select, func
    from app.database import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        existing_count = (
            await session.execute(select(func.count(User.username)))
        ).scalar_one_or_none() or 0
    if existing_count > 0:
        return
    logger = logging.getLogger(__name__)
    logger.warning(
        "No users found in database. Bootstrap an admin with:\n"
        "    docker compose exec backend python -m app.cli create-admin\n"
        "Or non-interactively (e.g., from a provisioning script):\n"
        "    AUTOTEST_ADMIN_USERNAME=alice AUTOTEST_ADMIN_PASSWORD='<at-least-8-chars>' \\\n"
        "    docker compose exec -T backend python -m app.cli create-admin --non-interactive"
    )


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
    try:
        await _seed_default_org_and_backfill()
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning("seed default org / backfill failed: %s", e)
    try:
        await _warn_if_no_users()
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning("user existence check failed: %s", e)
    scheduler_task = asyncio.create_task(scheduler_loop())
    # Sprint 10.1 — MCP idle sweeper
    from app.routers.ai import _mcp_idle_sweeper_loop
    mcp_sweeper_task = asyncio.create_task(_mcp_idle_sweeper_loop())
    try:
        yield
    finally:
        # Shutdown:停掉所有背景 task
        for t in (scheduler_task, mcp_sweeper_task):
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass


# RFC-8: observability bootstrap. Each call no-ops when its env switch is unset
# (PROM_DISABLED, OTLP_ENDPOINT, SENTRY_DSN) so dev runs stay quiet.
install_sentry("backend")
install_tracing()

app = FastAPI(
    title="AutoTest v1.0 API",
    description="企業級自動化測試平台後端 API",
    version="1.0.0",
    lifespan=lifespan,
)

install_metrics(app)
instrument_app(app)

# CORS 白名單:讀環境變數 ALLOWED_ORIGINS(逗號分隔),預設 http://localhost。
# 公開部署時必須設為實際前端 origin,不可使用 "*";allow_credentials=True 配 "*" 也會被瀏覽器拒絕。
_allowed_origins_raw = os.environ.get("ALLOWED_ORIGINS", "http://localhost").strip()
_allowed_origins = [o.strip() for o in _allowed_origins_raw.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Rate limiting：必須在 AuthMiddleware「之後 add」，這樣 dispatch 順序是
# Auth → SlowAPI → Audit → handler；slowapi 會看到 request.state.user_payload
# 來把 default key 從 IP 改成 user:<username>。
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)

# Audit log middleware：要在 Auth 之前 add（add 順序與執行順序相反，
# 所以 dispatch 順序為 Auth → Audit → handler）；確保 Audit 能看到 user_payload
app.add_middleware(AuditMiddleware)
# Auth middleware：在 CORS 之後加，確保 OPTIONS 預檢已被 CORS 處理
app.add_middleware(AuthMiddleware)

# /pics/* 由 nginx 直接反代到 SeaweedFS(pic bucket),backend 不再服務本地檔案。
# (歷史:STORAGE_BACKEND=local 時使用過 StaticFiles,改全 SeaweedFS 後移除)

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
app.include_router(todo_links.router,      prefix="/api", tags=["T · 待辦"])
app.include_router(auth.router,            prefix="/api", tags=["U · 認證"])
app.include_router(ai.router,              prefix="/api", tags=["V · AI"])
app.include_router(ai_chat.router,          prefix="/api", tags=["V · AI"])
app.include_router(audit_logs.router,      prefix="/api", tags=["W · 審計"])
app.include_router(organizations.router,   prefix="/api", tags=["X · 組織"])
app.include_router(notifications.router,   prefix="/api", tags=["Y · 通知"])
app.include_router(oidc.router,            prefix="/api")
app.include_router(mock_endpoints.router,  prefix="/api", tags=["Z · Mock 端點"])
app.include_router(db_configs.router,      prefix="/api", tags=["AA · DB 連線"])
app.include_router(groups.router,          prefix="/api", tags=["S · 設定"])
app.include_router(test_versions.router,   prefix="/api", tags=["TV · 測試版號"])
app.include_router(reviews.router,         prefix="/api", tags=["AB · 審核"])


@app.get("/", tags=["Health"])
async def root():
    return {"status": "ok", "service": "AutoTest v1.0 API"}


# RFC-8: split health probes.
#   /healthz  — liveness only (process up, no I/O). K8s livenessProbe.
#   /readyz   — readiness with DB + Valkey check. K8s readinessProbe; an
#               unready replica is yanked from the LB but not restarted.
@app.get("/healthz", tags=["Health"], include_in_schema=False)
async def healthz():
    return {"status": "ok"}


@app.get("/readyz", tags=["Health"], include_in_schema=False)
async def readyz():
    from sqlalchemy import text
    from app.database import engine

    checks: dict[str, str] = {}
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        checks["postgres"] = "ok"
    except Exception as exc:  # noqa: BLE001
        checks["postgres"] = f"fail: {exc}"

    try:
        from redis import asyncio as aioredis

        client = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
        await client.ping()
        checks["valkey"] = "ok"
    except Exception as exc:  # noqa: BLE001
        checks["valkey"] = f"fail: {exc}"

    failed = {k: v for k, v in checks.items() if v != "ok"}
    if failed:
        from fastapi.responses import JSONResponse

        return JSONResponse({"status": "not_ready", "checks": checks}, status_code=503)
    return {"status": "ready", "checks": checks}
