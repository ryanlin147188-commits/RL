import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# Root logger has no handlers by default in this image (uvicorn only configures
# its own uvicorn.* loggers). App-level logger.info/.warning calls would silently
# drop. Wire up a basic stderr handler at INFO so we can actually see them in
# `docker compose logs backend`.
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

from app.config import settings
from app.database import init_db
from app.routers import projects, tree_nodes, testcases, executions, reports, upload, import_export, recordings, local_runner, test_rounds, project_settings, screenshot_baselines, system, test_data_sets, settings as app_settings, todos, todo_links, auth, audit_logs, organizations, notifications, mock_endpoints, groups, reviews, artifacts, entity_versions, oidc_auth, project_role_permissions, shell_exec, schedules, defects, test_schedules
# v1.1.5:Casdoor sidecar 下架,OIDC 改 in-process(authlib + Zoho),由
# ``oidc_auth`` router 承接。舊的 ``oidc`` / ``casdoor_*`` 模組已刪除。
# 確保新增 model 在 init_db() 前已 import 註冊到 Base.metadata
from app.models import (  # noqa: F401

    TestDataSet,
    Role, NotificationPreference, Notification, EmailConfig, TodoItem, TodoLink, User,
    Organization, AuditLog, OidcProvider,
    MockEndpoint,
    Group, GroupMembership,
    OrgInvite,
    TestVersion,
)
from app.models.schedule import Schedule, RepeatType  # noqa: F401
from app.middleware import AuthMiddleware
from app.audit import AuditMiddleware
from app.rate_limit import limiter
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware


async def _seed_default_roles() -> None:
    """確保系統內建角色存在;不存在才建立。

    Phase 1 簡化版:只保留兩個系統角色
    * ``admin`` — 完整權限(包含既有 catalog 25 個 + 新矩陣 48 個)
    * ``user``  — 預設只能查看(12 個 *.read)

    舊角色 (Admin / QA / Viewer / Project-Admin / Project-Tester /
    Project-Reviewer / Project-Viewer) 已透過一次性 SQL migration 刪除,
    這支 seed 只負責「不存在才建立」,不會把舊角色長回來。
    """
    from sqlalchemy import select
    from app.database import AsyncSessionLocal

    # 新矩陣權限:12 個資源 × 4 個動作 (read/create/update/delete) = 48 個
    _MATRIX_RESOURCES = [
        "testcase", "testdata", "envvar", "testrun", "schedule", "recording",
        "report", "review", "filecompare", "project", "user", "role",
    ]
    _MATRIX_ACTIONS = ["read", "create", "update", "delete"]
    _matrix_perms = [f"{r}.{a}" for r in _MATRIX_RESOURCES for a in _MATRIX_ACTIONS]

    # 既有 catalog 的特殊權限(非 CRUD,例如 testcase.execute / plan.approve);
    # 為了不破壞既有 endpoint 的 require_casbin,admin 全部留著。
    _legacy_admin_perms = [
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
        "review.read", "review.submit", "review.manage",
    ]

    admin_perms = sorted(set(_matrix_perms) | set(_legacy_admin_perms))
    user_perms = [f"{r}.read" for r in _MATRIX_RESOURCES]

    DEFAULTS = [
        {
            "name": "admin",
            "scope": "org",
            "description": "系統管理員 — 完整權限",
            "permissions_json": admin_perms,
        },
        {
            "name": "user",
            "scope": "org",
            "description": "一般使用者 — 預設只能查看",
            "permissions_json": user_perms,
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
            else:
                # 既有 system role:對齊最新 permissions 與 scope
                # (避免使用者意外把 admin / user 的權限改壞,啟動時自動 heal)
                if existing.is_system:
                    existing.permissions_json = spec["permissions_json"]
                if existing.scope != spec["scope"]:
                    existing.scope = spec["scope"]
        await session.commit()


async def _migrate_legacy_roles() -> None:
    """Phase 1 role simplification 一次性 migration:把舊 7 個角色合併成
    ``admin`` / ``user`` 兩個系統角色。

    舊角色:Admin / QA / Viewer / Project-Admin / Project-Tester /
            Project-Reviewer / Project-Viewer
    新角色:admin / user(由 _seed_default_roles 建立)

    動作(全部 idempotent):
    1. 把舊 ``Admin`` 角色重命名為 ``admin`` (若 admin 已存在則跳過,改刪 Admin)
    2. 把所有非 superuser 且 role_id 指向舊 6 個角色的 user → reassign 到 user role
    3. 把 superuser + 指向舊角色的 user → reassign 到 admin role
    4. 刪除舊 6 個角色(role_id FK 已全部解開)

    完成後 ``roles`` 表只剩 admin / user + 任何使用者自訂角色。
    """
    from sqlalchemy import select, delete, update
    from app.database import AsyncSessionLocal

    LEGACY_NAMES = [
        "Admin", "QA", "Viewer",
        "Project-Admin", "Project-Tester", "Project-Reviewer", "Project-Viewer",
    ]

    async with AsyncSessionLocal() as session:
        # 取得 admin / user role(可能 _seed_default_roles 還沒跑;先試,沒有就 return)
        admin_role = (await session.execute(
            select(Role).where(Role.name == "admin", Role.is_system.is_(True))
        )).scalar_one_or_none()
        user_role = (await session.execute(
            select(Role).where(Role.name == "user", Role.is_system.is_(True))
        )).scalar_one_or_none()
        if not admin_role or not user_role:
            return  # seed 還沒跑,下次 startup 再試

        # 找出所有舊角色
        legacy_rows = (await session.execute(
            select(Role).where(Role.name.in_(LEGACY_NAMES))
        )).scalars().all()
        if not legacy_rows:
            return  # 已 migrate 完
        legacy_ids = [r.id for r in legacy_rows]

        # Reassign 使用者:superuser → admin role,其他 → user role
        await session.execute(
            update(User)
            .where(User.role_id.in_(legacy_ids), User.is_superuser.is_(True))
            .values(role_id=admin_role.id)
        )
        await session.execute(
            update(User)
            .where(User.role_id.in_(legacy_ids), User.is_superuser.is_(False))
            .values(role_id=user_role.id)
        )

        # 把 OrgMembership / ProjectMember 等其他可能參照的表也清掉舊 role_id
        # (set NULL,FK 不死綁;Casbin sync 會在下次 enforce 時重建)
        try:
            from app.models.org_membership import OrgMembership
            await session.execute(
                update(OrgMembership)
                .where(OrgMembership.role_id.in_(legacy_ids))
                .values(role_id=None)
            )
        except Exception:
            pass
        try:
            from app.models.project_member import ProjectMember
            await session.execute(
                update(ProjectMember)
                .where(ProjectMember.role_id.in_(legacy_ids))
                .values(role_id=None)
            )
        except Exception:
            pass

        # 最後刪除舊角色
        await session.execute(delete(Role).where(Role.id.in_(legacy_ids)))
        await session.commit()

        import logging
        logging.getLogger(__name__).info(
            "legacy roles migrated: %d removed (%s)",
            len(legacy_rows), ", ".join(r.name for r in legacy_rows),
        )


async def _backfill_org_wide_project_members() -> None:
    """仁慈模式 backfill:每個 organization 內所有 active user 自動成為該 org 所有
    project 的 ProjectMember。idempotent — 已存在就跳過,只 INSERT 缺的組合。

    Why this exists:
        list_projects 對 non-superuser 用 INNER JOIN ProjectMember 過濾,沒 row
        就看不到專案。OIDC JIT 建的 user / 早期建立但沒被 grandfather 的 user
        會撞上「進來什麼都看不到」。透過 startup backfill 把歷史殘留組合補齊。

    新建 user / project 的 forward path 已由 ``ensure_user_in_org_projects`` /
    ``ensure_project_has_all_org_users`` 處理,不會再有新缺洞。
    """
    from sqlalchemy import select
    from app.database import AsyncSessionLocal
    from app.models.user import User
    from app.models.project import Project
    from app.models.project_member import ProjectMember
    import logging

    log = logging.getLogger(__name__)
    async with AsyncSessionLocal() as session:
        # 一次取 (organization_id → [project_id...]) 與 (organization_id → [username...])
        proj_rows = (await session.execute(
            select(Project.id, Project.organization_id).where(Project.organization_id.isnot(None))
        )).all()
        user_rows = (await session.execute(
            select(User.username, User.organization_id).where(
                User.is_active.is_(True),
                User.organization_id.isnot(None),
            )
        )).all()
        if not proj_rows or not user_rows:
            return
        org_to_projects: dict[str, list[str]] = {}
        for pid, oid in proj_rows:
            org_to_projects.setdefault(oid, []).append(pid)
        org_to_users: dict[str, list[str]] = {}
        for uname, oid in user_rows:
            org_to_users.setdefault(oid, []).append(uname)

        # 找出已存在的 (project_id, username) tuple,避免重複 INSERT
        existing = set(
            (pid, uname) for pid, uname in (await session.execute(
                select(ProjectMember.project_id, ProjectMember.username)
            )).all()
        )

        added = 0
        for org_id, pids in org_to_projects.items():
            usernames = org_to_users.get(org_id, [])
            for pid in pids:
                for uname in usernames:
                    if (pid, uname) in existing:
                        continue
                    session.add(ProjectMember(
                        project_id=pid,
                        username=uname,
                        role_id=None,
                        status="active",
                    ))
                    added += 1
        if added:
            await session.commit()
            log.info("org-wide project_members backfill: added %d rows", added)


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
                select(Role).where(Role.name == "admin", Role.is_system.is_(True))
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
                select(Role).where(Role.name == "admin", Role.is_system.is_(True))
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


async def _backfill_defect_reviews() -> None:
    """補做 v1.1.9 之前建立、還沒對應 ReviewRecord 的 Defect。

    idempotent:用 LEFT JOIN 找缺少的,逐筆 INSERT。失敗單筆不擋其他;
    review_service.submit 不適用(它依賴 current_username / current_org_id
    context vars,lifespan 階段沒設),所以這裡直接構 ReviewRecord 寫 DB。
    """
    from datetime import datetime
    from sqlalchemy import select
    from app.database import AsyncSessionLocal
    from app.models.defect import Defect
    from app.models.review import ReviewRecord, ReviewableEntityType, ReviewStatus

    async with AsyncSessionLocal() as session:
        # 已存在的 defect review_record entity_id set
        existing_ids = set(
            (await session.execute(
                select(ReviewRecord.entity_id).where(
                    ReviewRecord.entity_type == ReviewableEntityType.DEFECT
                )
            )).scalars().all()
        )
        defects = (await session.execute(select(Defect))).scalars().all()
        missing = [d for d in defects if d.id not in existing_ids]
        if not missing:
            return
        for d in missing:
            session.add(ReviewRecord(
                entity_type=ReviewableEntityType.DEFECT,
                entity_id=d.id,
                status=ReviewStatus.PENDING,
                submitted_by=d.reporter or "system",
                submitted_at=d.created_at or datetime.utcnow(),
                organization_id=d.organization_id,
            ))
        await session.commit()
        logging.getLogger(__name__).info(
            "[backfill] created %d defect review_record(s)", len(missing)
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    # uvicorn's own dictConfig (run during server bootstrap, after the import-
    # time basicConfig at the top of this module) leaves the root logger with
    # no handlers — so app-level logger.info/.warning calls get dropped on the
    # floor during request handling. Reapply with force=True here, AFTER
    # uvicorn finishes its own setup, so /api/* request logs are actually
    # visible in `docker compose logs backend`.
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        force=True,
    )
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
    # Bucket bootstrap:取代原 seaweedfs-init compose service(那個只跑兩行
    # `aws s3 mb` 就拉一份 amazon/aws-cli image)。backend 已內建 boto3,直接
    # 在 lifespan 用同一個 S3 client 建 bucket 即可。idempotent;失敗不擋
    # startup(seaweedfs 可能還在 race;真正用到 bucket 時 storage_service
    # 自己會 propagate 錯誤)。
    try:
        from app.services.storage_service import ensure_buckets

        await asyncio.to_thread(ensure_buckets)
    except Exception:
        logging.getLogger(__name__).exception(
            "ensure_buckets failed during startup; S3 backend may not be ready yet"
        )
    await init_db()
    try:
        await _seed_default_roles()
    except Exception as e:  # 不要因為 seed 失敗而擋住服務啟動
        logging.getLogger(__name__).warning("seed default roles failed: %s", e)
    # Phase 1 role simplification:把舊 7 個角色合併成 admin / user
    # 這個 step idempotent;migrate 完之後每次 startup 都 no-op。
    try:
        await _migrate_legacy_roles()
    except Exception as e:
        logging.getLogger(__name__).warning("legacy role migration failed: %s", e)
    try:
        await _seed_default_org_and_backfill()
    except Exception as e:
        # Used to swallow as warning. Promoted to logger.exception so the
        # stack trace lands in container logs — a missing default org
        # cascades into 500s on /api/auth/register, so silent failures
        # here are nasty to debug.
        logging.getLogger(__name__).exception(
            "seed default org / backfill failed: %s", e,
        )
    # 仁慈模式 project_members backfill:同 org 內所有 user × 該 org 所有 project
    # 的交集,缺 ProjectMember 的補上(idempotent — 已存在就跳過)。確保歷史殘留
    # user (例如本次 ryan 透過 OIDC 綁定前是密碼帳號) 也能看到 admin 建的 project。
    try:
        await _backfill_org_wide_project_members()
    except Exception as e:
        logging.getLogger(__name__).warning("org-wide project_members backfill failed: %s", e)
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
        logging.getLogger(__name__).exception(
            "default admin bootstrap failed: %s", e,
        )
    # v1.1.9 backfill:既存 Defect 補上對應的 ReviewRecord。
    # 新建的 defect 已由 install_review_autocreate before_flush hook 自動建,
    # 但 v1.1.9 升版前已存在的 defect 沒對應 review_record,從審核頁看不到。
    # idempotent:已存在 (entity_type=defect, entity_id=X) 的 review_record 跳過。
    try:
        await _backfill_defect_reviews()
    except Exception:
        logging.getLogger(__name__).exception("defect review backfill failed")
    # Casbin enforcer(opt-in via CASBIN_ENABLED=True)— 進程內單例,首個
    # request 進來前必須完成 init 否則 require_casbin 一律 deny。adapter 在
    # init 時會 auto-create ``casbin_rule`` 表,與既有 schema 共存。
    try:
        from app.auth import casbin as _casbin

        if _casbin.is_enabled():
            await asyncio.to_thread(_casbin.init_enforcer)
    except Exception:
        logging.getLogger(__name__).exception(
            "Casbin enforcer init failed; falling back to require_permission only"
        )
    # v1.1.7 Phase 5: 註冊 user_id dual-write listener,新 OrgMembership /
    # ProjectMember / GroupMembership / PasswordResetToken row 在 insert 前
    # 會自動把 username → users.id 寫入 shadow column。Phase 7 PK cutover 才
    # 能放心切。
    try:
        from app.auth.user_id_dualwrite import register_user_id_dualwrite_listeners

        register_user_id_dualwrite_listeners()
    except Exception:
        logging.getLogger(__name__).exception(
            "user_id dual-write listener registration failed"
        )
    # 啟動排程背景任務（每 30 秒掃一次 schedules 表）
    scheduler_task = None
    try:
        from app.services.schedule_service import scheduler_loop
        scheduler_task = asyncio.create_task(scheduler_loop())
    except Exception as e:
        logging.getLogger(__name__).warning("scheduler_loop 啟動失敗: %s", e)
    try:
        yield
    finally:
        if scheduler_task:
            scheduler_task.cancel()
            try:
                await scheduler_task
            except asyncio.CancelledError:
                pass
        try:
            from app.auth import casbin as _casbin
            _casbin.shutdown_enforcer()
        except Exception:
            logging.getLogger(__name__).exception("casbin shutdown_enforcer failed")



app = FastAPI(
    title="AutoTest v1.1 API",
    description="企業級自動化測試平台後端 API",
    version="1.1.1",
    lifespan=lifespan,
)


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
app.include_router(local_runner.router,    prefix="/api", tags=["G · 本機執行"])
app.include_router(test_rounds.router,     prefix="/api", tags=["H · 測試回合"])
app.include_router(project_settings.router, prefix="/api", tags=["I · 專案設定（環境變數 / 設備）"])
app.include_router(screenshot_baselines.router, prefix="/api", tags=["J · Screenshot Diff Baseline"])
app.include_router(system.router,          prefix="/api", tags=["K · 系統狀態"])
app.include_router(shell_exec.router,      prefix="/api", tags=["L · Shell 執行"])
app.include_router(test_data_sets.router,  prefix="/api", tags=["P · 測試資料集 (DDT)"])
app.include_router(app_settings.router,    prefix="/api", tags=["S · 設定"])
app.include_router(todos.router,           prefix="/api", tags=["T · 待辦"])
app.include_router(todo_links.router,      prefix="/api", tags=["T · 待辦"])
app.include_router(auth.router,            prefix="/api", tags=["U · 認證"])
app.include_router(audit_logs.router,      prefix="/api", tags=["W · 審計"])
app.include_router(organizations.router,   prefix="/api", tags=["X · 組織"])
app.include_router(notifications.router,   prefix="/api", tags=["Y · 通知"])
app.include_router(oidc_auth.router,       prefix="/api")
app.include_router(project_role_permissions.router, prefix="/api", tags=["G · 專案"])
app.include_router(mock_endpoints.router,  prefix="/api", tags=["Z · Mock 端點"])
app.include_router(groups.router,          prefix="/api", tags=["S · 設定"])
app.include_router(reviews.router,         prefix="/api", tags=["AB · 審核"])
app.include_router(defects.router,         prefix="/api", tags=["AC · 缺陷管理"])
app.include_router(test_schedules.router,  prefix="/api", tags=["AD · 測試時程"])
app.include_router(entity_versions.router, prefix="/api", tags=["AC · 版本歷史"])
app.include_router(schedules.router,       prefix="/api", tags=["F · 排程"])


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
