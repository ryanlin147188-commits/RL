import asyncio
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.database import init_db
from app.routers import projects, tree_nodes, testcases, executions, reports, upload, import_export, recordings, schedules, local_runner, test_rounds, project_settings, screenshot_baselines, system, defects, test_milestones, test_plans, requirements, test_data_sets, test_documents, wbs_items, settings as app_settings, todos, todo_links, auth, ai, ai_chat, audit_logs, organizations, oidc, notifications, mock_endpoints, db_configs, groups, test_versions, reviews, assignments, artifacts, entity_versions
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
    """確保系統內建角色存在;不存在才建立。
    * scope=org:Admin / QA / Viewer(套用全 org)
    * scope=project:Project-Admin / Project-Tester / Project-Reviewer / Project-Viewer
      (套用在 ProjectMember.role_id,override OrgMembership 的角色)
    """
    from sqlalchemy import select
    from app.database import AsyncSessionLocal

    DEFAULTS = [
        {
            "name": "Admin",
            "scope": "org",
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
            "scope": "org",
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
            "scope": "org",
            "description": "檢視者 — 只讀全部",
            "permissions_json": [
                "project.read", "testcase.read", "defect.read",
                "requirement.read", "plan.read", "wbs.read",
                "document.read", "report.read", "settings.read",
            ],
        },
        # ── Phase 3 多租戶:per-project 角色 ───────────────────────────────
        {
            "name": "Project-Admin",
            "scope": "project",
            "description": "專案管理員 — 該專案內全部權限(可加減成員)",
            "permissions_json": [
                "project.read", "project.write",
                "testcase.read", "testcase.write", "testcase.delete", "testcase.execute",
                "defect.read", "defect.write", "defect.delete",
                "requirement.read", "requirement.write",
                "plan.read", "plan.write", "plan.approve",
                "wbs.read", "wbs.write",
                "document.read", "document.write",
                "report.read",
                "user.manage",
            ],
        },
        {
            "name": "Project-Tester",
            "scope": "project",
            "description": "專案測試人員 — 寫案例 + 跑測試 + 缺陷",
            "permissions_json": [
                "project.read",
                "testcase.read", "testcase.write", "testcase.execute",
                "defect.read", "defect.write",
                "requirement.read",
                "plan.read",
                "wbs.read",
                "document.read",
                "report.read",
            ],
        },
        {
            "name": "Project-Reviewer",
            "scope": "project",
            "description": "專案審核者 — 讀全部 + 核准計畫",
            "permissions_json": [
                "project.read",
                "testcase.read",
                "defect.read",
                "requirement.read",
                "plan.read", "plan.approve",
                "wbs.read",
                "document.read",
                "report.read",
            ],
        },
        {
            "name": "Project-Viewer",
            "scope": "project",
            "description": "專案檢視者 — 只讀",
            "permissions_json": [
                "project.read", "testcase.read", "defect.read",
                "requirement.read", "plan.read", "wbs.read",
                "document.read", "report.read",
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
                    scope=spec["scope"],
                ))
            elif existing.scope != spec["scope"]:
                # 既有 row 把 scope 補回;permissions_json 不動,避免覆蓋使用者客製
                existing.scope = spec["scope"]
        await session.commit()


async def _heal_admin_user() -> None:
    """Self-heal the built-in `admin` account so every restart guarantees
    a working RBAC entry point.

    Why this exists:
        During development we sometimes flip is_superuser=false to test
        non-admin code paths or the roles table gets wiped. The next
        login then 403s because admin has no role and no superuser bit.
        Walking the user through SQL fixes is annoying. Promote idempotently
        on every startup so the seed is self-correcting.

    Does not create the admin user — `python -m app.cli create-admin` is
    still the entry point for first install. We only fix existing rows.
    """
    from sqlalchemy import select
    from app.database import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        admin = (
            await session.execute(select(User).where(User.username == "admin"))
        ).scalar_one_or_none()
        if not admin:
            return  # CLI bootstrap will handle creation
        admin_role = (
            await session.execute(
                select(Role).where(Role.name == "Admin", Role.is_system.is_(True))
            )
        ).scalar_one_or_none()

        changed = False
        if not admin.is_superuser:
            admin.is_superuser = True
            changed = True
        if not admin.is_active:
            admin.is_active = True
            changed = True
        if admin_role and admin.role_id != admin_role.id:
            admin.role_id = admin_role.id
            changed = True
        if changed:
            await session.commit()
            import logging
            logging.getLogger(__name__).info(
                "admin self-heal: superuser=%s active=%s role_id=%s",
                admin.is_superuser, admin.is_active, admin.role_id,
            )


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


async def _ensure_default_admin() -> None:
    """確保「預設系統管理員」一定存在;不存在就用 admin/admin123 種出來,
    並打開 must_change_password 旗標,使用者第一次登入會被前端強制改密碼。

    決策:
      * 帳號名固定 ``admin``;預設密碼來自 env ``AUTOTEST_DEFAULT_ADMIN_PASSWORD``
        (預設 ``admin123``)。Prod 環境可在 .env 設更強的初始密碼,容器
        起來後第一次登入仍會被強制改一次。
      * 既有 ``admin`` row 不會被覆蓋(連 must_change_password / password_hash
        都不動),避免重啟意外重置密碼。
      * 一併把 admin 掛到 default org + Admin role + is_superuser=True,
        相當於先前 _heal_admin_user() 的自我修復行為。
    """
    import logging
    import os
    from sqlalchemy import select
    from app.auth.security import hash_password
    from app.database import AsyncSessionLocal

    logger = logging.getLogger(__name__)
    default_password = (
        os.environ.get("AUTOTEST_DEFAULT_ADMIN_PASSWORD") or "admin123"
    )

    async with AsyncSessionLocal() as session:
        admin = (
            await session.execute(select(User).where(User.username == "admin"))
        ).scalar_one_or_none()
        admin_role = (
            await session.execute(
                select(Role).where(Role.name == "Admin", Role.is_system.is_(True))
            )
        ).scalar_one_or_none()
        default_org = (
            await session.execute(
                select(Organization).where(Organization.slug == "default")
            )
        ).scalar_one_or_none()

        if admin is None:
            admin = User(
                username="admin",
                display_name="系統管理員",
                password_hash=hash_password(default_password),
                role_id=admin_role.id if admin_role else None,
                organization_id=default_org.id if default_org else None,
                is_superuser=True,
                is_active=True,
                must_change_password=True,
            )
            session.add(admin)
            await session.commit()
            logger.warning(
                "Default admin created: username=admin password=%s "
                "(must change password on first login)",
                "admin123" if default_password == "admin123" else "<from env>",
            )
            return

        # 既有 admin → 只做 self-heal 不動密碼
        changed = False
        if not admin.is_superuser:
            admin.is_superuser = True
            changed = True
        if not admin.is_active:
            admin.is_active = True
            changed = True
        if admin_role and admin.role_id != admin_role.id:
            admin.role_id = admin_role.id
            changed = True
        if changed:
            await session.commit()
            logger.info(
                "admin self-heal: superuser=%s active=%s role_id=%s",
                admin.is_superuser, admin.is_active, admin.role_id,
            )


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup：建立 PIC 資料夾 + 自動建表 + 啟動排程背景任務
    os.makedirs(settings.PIC_FOLDER, exist_ok=True)
    # Storage backend is S3-only (SeaweedFS via S3-compatible API). The
    # earlier 'local' fallback wrote uploads to the container's filesystem
    # which got wiped on restart — proven config-incident magnet, so it's
    # gone in every environment now.
    _backend = (settings.STORAGE_BACKEND or "").strip().lower()
    if _backend != "s3":
        raise RuntimeError(
            f"STORAGE_BACKEND='{settings.STORAGE_BACKEND}' is not supported. "
            f"Set STORAGE_BACKEND=s3 in .env (only SeaweedFS-backed S3 is "
            f"available; the 'local' / 'minio' values from earlier versions "
            f"have been removed)."
        )
    await init_db()
    try:
        await _seed_default_roles()
    except Exception as e:  # 不要因為 seed 失敗而擋住服務啟動
        import logging
        logging.getLogger(__name__).warning("seed default roles failed: %s", e)
    try:
        await _seed_default_org_and_backfill()
    except Exception as e:
        # Used to swallow as warning. Promoted to logger.exception so the
        # stack trace lands in container logs — a missing default org
        # cascades into 500s on /api/auth/register, so silent failures
        # here are nasty to debug.
        import logging
        logging.getLogger(__name__).exception(
            "seed default org / backfill failed: %s", e,
        )
    # NOTE: previous versions ran `_heal_admin_user()` here to keep the
    # built-in `admin` account in working state across restarts. That's
    # been removed per product decision: ship with NO default admin and
    # NO default project. Operators bootstrap their first admin via
    # `docker compose exec backend python -m app.cli create-admin`
    # (or the /api/auth/bootstrap-invite flow when AUTOTEST_BOOTSTRAP_TOKEN
    # is set). The function itself is kept below for environments where
    # ops want to re-enable self-heal — just call it from here.
    try:
        await _ensure_default_admin()
    except Exception as e:
        import logging
        logging.getLogger(__name__).exception(
            "default admin bootstrap failed: %s", e,
        )
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
    title="AutoTest v1.1 API",
    description="企業級自動化測試平台後端 API",
    version="1.1.0",
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

# /pics/* and /results/* keep their public URL shape but are now served by
# backend so access can be gated by short-lived artifact tokens.
app.include_router(artifacts.router)

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
app.include_router(entity_versions.router, prefix="/api", tags=["AC · 版本歷史"])
app.include_router(assignments.router,     prefix="/api", tags=["AC · 指派"])


@app.get("/", tags=["Health"])
async def root():
    return {"status": "ok", "service": "AutoTest v1.1 API"}


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
